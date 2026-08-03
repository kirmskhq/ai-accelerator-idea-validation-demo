from langflow.custom import Component
from langflow.io import SecretStrInput, StrInput, SliderInput, Output
from langflow.field_typing import LanguageModel


class YandexChatModelComponent(Component):
    display_name = "Yandex Chat Model"
    description = "Yandex AI Studio chat model over its OpenAI-compatible endpoint. Supports tool calling, so Agents can use it."
    icon = "brain"
    name = "YandexChatModel"

    inputs = [
        SecretStrInput(name="api_key", display_name="Yandex API Key", required=True),
        StrInput(name="folder_id", display_name="Yandex Folder ID", required=True),
        StrInput(name="model", display_name="Model", value="yandexgpt/latest"),
        SliderInput(name="temperature", display_name="Temperature", value=0.4, range_spec={"min": 0.0, "max": 1.0, "step": 0.01}),
    ]

    outputs = [
        Output(display_name="Language Model", name="model_output", method="build_model"),
    ]

    def build_model(self) -> LanguageModel:
        from langchain_openai import ChatOpenAI

        model = self.model or "yandexgpt/latest"
        if not model.startswith(("gpt://", "ds://")):
            if "/" not in model:
                model = model + "/latest"
            model = "gpt://" + self.folder_id + "/" + model

        return ChatOpenAI(
            model=model,
            api_key=self.api_key,
            base_url="https://ai.api.cloud.yandex.net/v1",
            temperature=self.temperature,
            timeout=90,
        )
