from langflow.custom import Component
from langflow.io import SecretStrInput, StrInput, SliderInput, Output
from langflow.field_typing import LanguageModel


class YandexChatModelComponent(Component):
    """Chat model for the Agent over an OpenAI-compatible endpoint.

    Defaults to Yandex AI Studio, where model names are expanded to
    gpt://<folder>/<model>/latest. Any other OpenAI-compatible endpoint with
    tool calling works too (for example NeuralDeep): set API Base URL and pass
    the provider's model name as is; Folder ID is then not used.
    """

    display_name = "Yandex Chat Model"
    description = (
        "Chat model for the Agent over an OpenAI-compatible endpoint with tool "
        "calling. Defaults to Yandex AI Studio; set API Base URL for another provider."
    )
    icon = "brain"
    name = "YandexChatModel"

    YANDEX_BASE = "https://ai.api.cloud.yandex.net/v1"

    inputs = [
        SecretStrInput(name="api_key", display_name="API Key", required=True),
        StrInput(name="folder_id", display_name="Yandex Folder ID", required=False,
                 info="Yandex AI Studio only."),
        StrInput(name="model", display_name="Model", value="deepseek-v4-flash"),
        SliderInput(name="temperature", display_name="Temperature", value=0.4, range_spec={"min": 0.0, "max": 1.0, "step": 0.01}),
        StrInput(name="api_base", display_name="API Base URL", value=YANDEX_BASE, advanced=True,
                 info="OpenAI-compatible endpoint, e.g. https://api.neuraldeep.ru/v1"),
    ]

    outputs = [
        Output(display_name="Language Model", name="model_output", method="build_model"),
    ]

    def _is_yandex(self) -> bool:
        return "api.cloud.yandex.net" in (self.api_base or self.YANDEX_BASE)

    def _model_id(self, default: str) -> str:
        model = self.model or default
        if not self._is_yandex() or model.startswith(("gpt://", "ds://")):
            return model
        if not self.folder_id:
            raise ValueError("Yandex Folder ID is required for Yandex AI Studio models.")
        if "/" not in model:
            model = model + "/latest"
        return "gpt://" + self.folder_id + "/" + model

    def build_model(self) -> LanguageModel:
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=self._model_id("deepseek-v4-flash"),
            api_key=self.api_key,
            base_url=(self.api_base or self.YANDEX_BASE).rstrip("/"),
            temperature=self.temperature,
            timeout=90,
        )
