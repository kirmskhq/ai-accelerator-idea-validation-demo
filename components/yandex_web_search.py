from langflow.custom import Component
from langflow.io import SecretStrInput, StrInput, MessageTextInput, Output
from langflow.schema import Data
from langflow.schema.message import Message


class YandexWebSearchComponent(Component):
    """Market check: find working products that solve the founder's problem.

    Two backends, chosen by API Base URL:

    - Yandex AI Studio (default): a model with Yandex's built-in web_search
      tool; the URLs are the ones the search tool actually cited.
    - NeuralDeep (https://api.neuraldeep.ru/v1): its /search/web endpoint,
      then a model picks the actual products out of those results. Strict JSON
      limits its choice to URLs the search returned, so it cannot invent links.

    The count is the number of distinct websites among the chosen URLs: an
    honest proxy for "similar products found", not a verified competitor count.

    The result is also written to the interview's state session, so Verdict
    R1-R5 reads the count from there instead of relying on the agent to copy
    the number across.
    """

    display_name = "Yandex Web Search"
    description = "Finds working products similar to the founder's idea and returns them with links taken from real search results."
    icon = "search"
    name = "YandexWebSearch"

    STATE_SESSION_SUFFIX = "::interview-state"
    MARKET_SENDER_NAME = "__market_search__"
    YANDEX_BASE = "https://ai.api.cloud.yandex.net/v1"
    RESULTS_TO_READ = 10

    SEARCH_SYSTEM = "Ты ищешь в вебе работающие продукты и сервисы. Всегда приводи ссылки на найденное. Не выдумывай названий и адресов: только то, что реально нашёл."
    PICK_SYSTEM = (
        "Тебе дают результаты веб-поиска. Отбери из них только работающие продукты "
        "и сервисы, которые решают указанную задачу. Статьи, обзоры, подборки, "
        "магазины приложений, каталоги и форумы не считай. Если подходящих нет, "
        "верни пустой список."
    )

    inputs = [
        SecretStrInput(name="api_key", display_name="API Key", required=True),
        StrInput(name="folder_id", display_name="Yandex Folder ID", required=False,
                 info="Yandex AI Studio only."),
        StrInput(name="model", display_name="Model", value="yandexgpt/latest"),
        MessageTextInput(name="query", display_name="Search Query", required=True, tool_mode=True,
                         info="What to look for: the task the product solves plus the audience, in Russian, a short phrase."),
        StrInput(name="api_base", display_name="API Base URL", value=YANDEX_BASE, advanced=True,
                 info="Yandex AI Studio (default) or NeuralDeep: https://api.neuraldeep.ru/v1"),
    ]

    outputs = [
        Output(display_name="Result", name="result", method="search_market"),
    ]

    def _flow_uuid(self):
        # Inside a tool call Langflow hands components a placeholder graph whose
        # flow_id is a string; the message store needs a UUID.
        from uuid import UUID

        fid = getattr(self.graph, "flow_id", None)
        if isinstance(fid, UUID):
            return fid
        if isinstance(fid, str):
            try:
                return UUID(fid)
            except ValueError:
                return None
        return None

    def _base(self) -> str:
        return (self.api_base or self.YANDEX_BASE).rstrip("/")

    def _is_yandex(self) -> bool:
        return "api.cloud.yandex.net" in self._base()

    def _model_id(self) -> str:
        model = self.model or "yandexgpt/latest"
        if not self._is_yandex() or model.startswith(("gpt://", "ds://")):
            return model
        if not self.folder_id:
            raise ValueError("Yandex Folder ID is required for Yandex AI Studio models.")
        if "/" not in model:
            model = model + "/latest"
        return "gpt://" + self.folder_id + "/" + model

    @staticmethod
    def _site(url: str) -> str:
        from urllib.parse import urlparse

        host = (urlparse(url).netloc or "").lower().split(":")[0]
        return host[4:] if host.startswith("www.") else host

    def _record(self, result: dict) -> None:
        import json
        from langflow.memory import store_message

        msg = Message(
            text=json.dumps(result, ensure_ascii=False),
            sender="Machine",
            sender_name=self.MARKET_SENDER_NAME,
            session_id=str(self.graph.session_id) + self.STATE_SESSION_SUFFIX,
        )
        try:
            store_message(msg, flow_id=self._flow_uuid())
        except Exception as exc:
            err = "Не удалось сохранить результат поиска: " + str(exc)
            raise ValueError(err) from exc

    def _search_yandex(self, client, query: str) -> tuple:
        prompt = ("Найди работающие продукты и сервисы, которые решают эту задачу: " + query +
                  " Ищи короткими запросами из 3–7 слов. Перечисли найденное со ссылками. "
                  "Статьи, обзоры и подборки «10 лучших» не нужны — нужны сами продукты.")
        resp = client.responses.create(
            model=self._model_id(),
            instructions=self.SEARCH_SYSTEM,
            input=prompt,
            tools=[{"type": "web_search", "filters": {"allowed_domains": []}, "search_context_size": "medium"}],
        )
        urls = []
        for item in getattr(resp, "output", None) or []:
            for chunk in getattr(item, "content", None) or []:
                for ann in getattr(chunk, "annotations", None) or []:
                    url = getattr(ann, "url", None)
                    if url and url not in urls:
                        urls.append(url)
        return (resp.output_text or "").strip(), urls

    def _search_neuraldeep(self, client, query: str) -> tuple:
        import json
        import httpx

        resp = httpx.post(
            self._base() + "/search/web",
            headers={"Authorization": "Bearer " + str(self.api_key)},
            json={"query": query, "limit": self.RESULTS_TO_READ},
            timeout=60,
        )
        resp.raise_for_status()
        results = [r for r in (resp.json().get("results") or []) if r.get("url")]
        if not results:
            return "Поиск не вернул результатов.", []

        listing = "\n".join(
            str(i + 1) + ". " + (r.get("title") or "") + " — " + r["url"] + "\n   " + (r.get("content") or "")[:300]
            for i, r in enumerate(results)
        )
        schema = {
            "type": "object",
            "properties": {
                "products": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "url": {"type": "string", "enum": [r["url"] for r in results]},
                        },
                        "required": ["name", "url"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["products"],
            "additionalProperties": False,
        }
        pick = client.chat.completions.create(
            model=self._model_id(),
            temperature=0,
            messages=[
                {"role": "system", "content": self.PICK_SYSTEM},
                {"role": "user", "content": "Задача: " + query + "\n\nРезультаты поиска:\n" + listing},
            ],
            response_format={"type": "json_schema", "json_schema": {"name": "products", "schema": schema, "strict": True}},
        )
        products = json.loads(pick.choices[0].message.content or "{}").get("products") or []
        urls = []
        for p in products:
            if p.get("url") and p["url"] not in urls:
                urls.append(p["url"])
        text = "\n".join("- " + (p.get("name") or "") + ": " + p["url"] for p in products) or "Похожих продуктов не найдено."
        return text, urls

    def search_market(self) -> Data:
        import openai

        query = self.query
        query = query.text if hasattr(query, "text") else str(query)

        client = openai.OpenAI(
            api_key=self.api_key,
            base_url=self._base(),
            project=self.folder_id if self._is_yandex() else None,
            timeout=90,
        )
        try:
            if self._is_yandex():
                text, urls = self._search_yandex(client, query)
            else:
                text, urls = self._search_neuraldeep(client, query)
        except Exception as exc:
            result = {"error": str(exc)[:300], "query": query, "text": "", "urls": [], "count": -1}
            self._record(result)
            self.status = "Search error: " + str(exc)
            return Data(data=result)

        sites = sorted({self._site(u) for u in urls if self._site(u)})
        result = {"query": query, "text": text, "urls": urls, "sites": sites, "count": len(sites)}
        self._record(result)
        self.status = result
        return Data(data=result)
