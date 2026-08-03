from langflow.custom import Component
from langflow.io import SecretStrInput, StrInput, MessageTextInput, Output
from langflow.schema import Data


class YandexWebSearchComponent(Component):
    display_name = "Yandex Web Search"
    description = "Searches the web via Yandex AI Studio's built-in search tool and returns working products and services with real links (mirrors core/llm.py:web_search())."
    icon = "search"
    name = "YandexWebSearch"

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
            self.status = "Yandex error: " + str(exc)
            return Data(data={"error": str(exc), "text": "", "urls": [], "count": -1})

        urls = []
        for item in getattr(resp, "output", None) or []:
            for chunk in getattr(item, "content", None) or []:
                for ann in getattr(chunk, "annotations", None) or []:
                    url = getattr(ann, "url", None)
                    if url and url not in urls:
                        urls.append(url)

        text = (resp.output_text or "").strip()
        result = {"text": text, "urls": urls, "count": len(urls)}
        self.status = result
        return Data(data=result)
