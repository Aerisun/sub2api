"""Select optional server tools from the human's request, not tool data."""
import re

_GENERATION = re.compile(
    r'(?:^|[，,。!?！？;；\n]|请|帮我|帮忙|为我|给我|想|需要|能否|可以|再|重新|并|然后)\s*'
    r'(?:画(?!面|风|质|法)|绘制|绘画|重画|重绘|渲染|生图)|'
    r'(?:生成|创作|制作|创建|设计|编辑|修改|修复|替换)[^。!?！？;；\n]{0,48}'
    r'(?:图片|图像|照片|插画|头像|海报|封面|壁纸|图标|画作|logo)|'
    r'(?:给我|来|要)(?:一|两|几)?张[^。!?！？;；\n]{0,40}(?:图片|照片|插画|海报|壁纸)|'
    r'\b(?:give me|show me|send me|i would like|i.d like|i want|i need)\b[^.!?;\n]{0,40}'
    r'\b(?:images?|pictures?|photos?|illustrations?|avatars?|posters?|wallpapers?|logos?|icons?)\b|'
    r'\b(?:draw|paint|illustrate)(?:\s+\S|[.!?]?$)|'
    r'\b(?:generate|create|make|design|edit|modify|render|redraw)\b[^.!?;\n]{0,80}'
    r'\b(?:images?|pictures?|photos?|illustrations?|avatars?|posters?|wallpapers?|logos?|icons?)\b|'
    r'(?:画像|イラスト|写真|絵).{0,20}(?:生成|作成|描)|描いて|그려|нарисуй|ارسم|dibuja|dessine|zeichne|disegna|desenha', re.I)
_TEXT_TASK = re.compile(
    r'^\s*(?:(?:请|帮我|麻烦|please)\s*)?(?:计算|求解|推导|证明|解释|翻译|总结|分析|识别|描述|介绍|如何|怎么|'
    r'写|编写|实现|撰写|how\b|what\b|why\b|explain\b|translate\b|summari[sz]e\b|analy[sz]e\b|describe\b|recognize\b|read\b|calculate\b|solve\b|write\b|debug\b|review\b|implement\b)', re.I)
_ALSO_GENERATE = re.compile(r'(?:并|同时|然后|另外|and(?:\s+then)?|also)\s*(?:请|帮我)?\s*(?:画|生成|绘制|制作|给我|create|generate|draw|paint|show|give)', re.I)
_NO_IMAGE = re.compile(r'(?:不要|别|无需|不用).{0,8}(?:生图|生成.{0,8}(?:图|像)|画图|绘图)|\b(?:do not|don.t|without)\s+(?:generate|create|draw|make)\b', re.I)
_EDIT = re.compile(r'改|换|变|调整|编辑|修|去掉|移除|替换|背景|颜色|风格|再来.{0,4}张|\b(?:edit|change|regenerate|another|remove|replace|background|colou?r|style)\b|make it|one more', re.I)
_CALCULATION = re.compile(r'^\s*(?:(?:请|帮我|麻烦|please)\s*)?(?:计算|求解|calculate|compute|evaluate|solve)\s*[:：]?\s*[0-9(（][0-9\s.+*/×÷()（）^%−-]{1,100}', re.I)
_EXTERNAL = re.compile(r'搜索|联网|最新|查证|核实|查一下|搜一下|\b(?:search|browse|verify|current|latest|web|look up)\b', re.I)


def _human_text(message, kind):
    if not isinstance(message, dict) or message.get('role') != 'user':
        return ''
    content = message.get('parts' if kind == 'gemini' else 'content', '')
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = '\n'.join(part.get('text', '') for part in content if isinstance(part, dict)
                         and part.get('type', 'text') in ('text', 'input_text') and isinstance(part.get('text'), str))
    else:
        return ''
    # Recognition output is untrusted image content, not a new human command.
    text = text.split('\n\n[图片识别结果]\n', 1)[0]
    if len(text) > 32768:
        text = text[:16384]+'\n'+text[-16384:]
    return text.strip()


def _has_image_context(messages):
    pending = list(messages)
    for _ in range(256):
        if not pending:
            break
        item = pending.pop()
        if isinstance(item, dict):
            if any(key in item for key in ('image_url', 'input_image')) or item.get('type') in ('image', 'input_image'):
                return True
            if str(item.get('mimeType') or item.get('mime_type') or '').startswith('image/'):
                return True
            pending.extend(list(item.values())[:32])
        elif isinstance(item, list):
            pending.extend(item[-32:])
        elif isinstance(item, str):
            bounded = item[:16384]+item[-16384:]
            if any(marker in bounded for marker in ('aerisun_generate_image', 'image_generation', '/image-hub/results/')):
                return True
    return False


def wants_image_generation(body, kind):
    history = body.get('contents' if kind == 'gemini' else 'input' if kind == 'responses' else 'messages', [])
    if isinstance(history, str):
        history = [{'role': 'user', 'content': history}]
    if not isinstance(history, list):
        return False
    human = [(index, _human_text(message, kind)) for index, message in enumerate(history)]
    human = [(index, text) for index, text in human if text]
    if not human:
        return False
    index, text = human[-1]
    if _NO_IMAGE.search(text) or _TEXT_TASK.search(text) and not _ALSO_GENERATE.search(text):
        return False
    if _GENERATION.search(text):
        return True
    if not _EDIT.search(text):
        return False
    context = history[max(0, index-8):index]
    previous_images = any(_GENERATION.search(_human_text(message, kind)) for message in context)
    return bool(previous_images or _has_image_context(context+[history[index]]))


def self_contained_calculation(body, kind):
    history = body.get('contents' if kind == 'gemini' else 'input' if kind == 'responses' else 'messages', [])
    if isinstance(history, str):
        history = [{'role': 'user', 'content': history}]
    if not isinstance(history, list):
        return False
    for message in reversed(history):
        text = _human_text(message, kind)
        if text:
            return bool(_CALCULATION.search(text) and not _EXTERNAL.search(text))
    return False
