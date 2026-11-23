from langflow.custom import Component
from langflow.io import SecretStrInput, StrInput, MessageTextInput, Output
from langflow.schema import Data
from langflow.schema.message import Message


class YandexWebSearchComponent(Component):
    """Market check through Yandex AI Studio's built-in web search tool.

    Returns the model's summary plus the URLs the search tool actually cited.
    The count is the number of distinct websites among those URLs: an honest
    proxy for "similar products found", not a verified competitor count.

    The result is also written to the interview's state session, so Verdict
    R1-R5 reads the count from there instead of relying on the agent to copy
    the number across.
    """

    display_name = "Yandex Web Search"
    description = "Searches the web via Yandex AI Studio's built-in search tool and returns similar products with the links the search actually cited."
    icon = "search"
    name = "YandexWebSearch"

    STATE_SESSION_SUFFIX = "::interview-state"
    MARKET_SENDER_NAME = "__market_search__"

    SEARCH_SYSTEM = "Ты ищешь в вебе работающие продукты и сервисы. Всегда приводи ссылки на найденное. Не выдумывай названий и адресов: только то, что реально нашёл."

    inputs = [
        SecretStrInput(name="api_key", display_name="Yandex API Key", required=True),
        StrInput(name="folder_id", display_name="Yandex Folder ID", required=True),
        StrInput(name="model", display_name="Model", value="yandexgpt/latest"),
        MessageTextInput(name="query", display_name="Search Query", required=True, tool_mode=True,
                         info="What to look for: the task the product solves plus the audience, in Russian, a short phrase."),
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

    def search_market(self) -> Data:
        import openai

        query = self.query
        query = query.text if hasattr(query, "text") else str(query)

        model = self.model or "yandexgpt/latest"
        if not model.startswith(("gpt://", "ds://")):
            if "/" not in model:
                model = model + "/latest"
            model = "gpt://" + self.folder_id + "/" + model

        client = openai.OpenAI(api_key=self.api_key, base_url="https://ai.api.cloud.yandex.net/v1", project=self.folder_id, timeout=90)
        prompt = ("Найди работающие продукты и сервисы, которые решают эту задачу: " + query +
                  " Ищи короткими запросами из 3–7 слов. Перечисли найденное со ссылками. "
                  "Статьи, обзоры и подборки «10 лучших» не нужны — нужны сами продукты.")
        try:
            resp = client.responses.create(
                model=model,
                instructions=self.SEARCH_SYSTEM,
                input=prompt,
                tools=[{"type": "web_search", "filters": {"allowed_domains": []}, "search_context_size": "medium"}],
            )
        except Exception as exc:
            result = {"error": str(exc)[:300], "query": query, "text": "", "urls": [], "count": -1}
            self._record(result)
            self.status = "Yandex error: " + str(exc)
            return Data(data=result)

        urls = []
        for item in getattr(resp, "output", None) or []:
            for chunk in getattr(item, "content", None) or []:
                for ann in getattr(chunk, "annotations", None) or []:
                    url = getattr(ann, "url", None)
                    if url and url not in urls:
                        urls.append(url)

        sites = sorted({self._site(u) for u in urls if self._site(u)})
        text = (resp.output_text or "").strip()
        result = {"query": query, "text": text, "urls": urls, "sites": sites, "count": len(sites)}
        self._record(result)
        self.status = result
        return Data(data=result)
