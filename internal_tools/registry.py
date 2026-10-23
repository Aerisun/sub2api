"""One catalogue and executor registry for authenticated REST and MCP."""
from copy import deepcopy
from internal_tools.errors import ToolError
from internal_tools.web_search import DEFINITION as SEARCH_DEFINITION, WebSearchExecutor, authorized_models, search_model
from internal_tools.image_tools import (GENERATION, RECOGNITION, GENERATION_DEFINITION, RECOGNITION_DEFINITION,
                                        ImageGenerationExecutor, ImageRecognitionExecutor, can_generate, can_recognize)

DEFINITIONS = (GENERATION_DEFINITION, RECOGNITION_DEFINITION, SEARCH_DEFINITION)
NAMES = frozenset(item["name"] for item in DEFINITIONS)


def catalogue(credential,gateway,headers=None):
    models=authorized_models(credential,gateway,headers)
    allowed={"web_search":search_model(models) is not None,GENERATION:can_generate(models),RECOGNITION:can_recognize(models)}
    return [deepcopy(item) for item in DEFINITIONS if allowed[item["name"]]]


def execute(name,arguments,call_id,credential,gateway,headers=None,idempotency=None,hub="http://127.0.0.1:8092",progress=None):
    if name=="web_search":executor=WebSearchExecutor(credential,gateway,headers)
    elif name==GENERATION:executor=ImageGenerationExecutor(credential,gateway,headers,hub,idempotency)
    elif name==RECOGNITION:executor=ImageRecognitionExecutor(credential,gateway,headers,progress=progress)
    else:raise ToolError(400,"未知工具；支持 image_generation、image_recognition、web_search","unknown_tool",param="name")
    return executor(arguments,call_id)
