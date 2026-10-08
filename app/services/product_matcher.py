import re
from dataclasses import dataclass, field
from typing import Any


GENERIC_BRANDS = {"", "no name", "noname", "нет бренда", "без бренда", "brand"}


TYPE_RULES = (
    ("phone_case", "phone_accessory", ("чехол", "чехл", "бампер", "накладк", "phone case", "case for iphone")),
    ("screen_protector", "phone_accessory", ("защитн стекл", "стекло для телефон", "screen protector")),
    ("phone_battery", "phone_accessory", ("аккумуляторн батар", "аккумулятор для iphone", "батарея для телефон", "phone battery")),
    ("charger", "phone_accessory", ("зарядн устрой", "зарядк", "charger", "адаптер питан")),
    ("cable", "phone_accessory", ("кабел", "провод usb", "usb cable", "lightning cable")),
    ("smartphone", "phone", ("смартфон", "мобильн телефон", "smartphone")),
    ("smart_speaker", "audio", ("умн колонк", "яндекс станц", "smart speaker", "алиса колонк")),
    ("headphones", "audio", ("наушник", "гарнитур", "headphone", "earbuds", "airpods")),
    ("speaker", "audio", ("колонк", "портативн акуст", "bluetooth speaker")),
    ("laptop", "computer", ("ноутбук", "laptop", "macbook", "ultrabook")),
    ("tablet", "computer", ("планшет", "tablet", "ipad")),
    ("smartwatch", "wearable", ("умн час", "смарт час", "smart watch", "smartwatch")),
    ("television", "electronics", ("телевизор", "smart tv", "телевизионн панел")),
    ("monitor", "computer", ("монитор", "display monitor")),
    ("keyboard", "computer_accessory", ("клавиатур", "keyboard")),
    ("computer_mouse", "computer_accessory", ("компьютерн мыш", "игров мыш", "computer mouse")),
    ("vacuum", "home_appliance", ("пылесос", "vacuum cleaner")),
    ("washing_machine", "home_appliance", ("стиральн машин", "washing machine")),
    ("refrigerator", "home_appliance", ("холодильник", "refrigerator")),
    ("microwave", "home_appliance", ("микроволнов", "свч печ", "microwave")),
    ("kettle", "kitchen_appliance", ("электрочайник", "электрическ чайник", "kettle")),
    ("coffee_machine", "kitchen_appliance", ("кофемашин", "кофеварк", "coffee machine")),
    ("blender", "kitchen_appliance", ("блендер", "blender")),
    ("iron", "home_appliance", ("утюг", "отпаривател", "iron")),
    ("hair_dryer", "beauty_device", ("фен", "стайлер", "hair dryer")),
    ("sneakers", "footwear", ("кроссов", "кед", "sneaker", "trainers")),
    ("boots", "footwear", ("ботин", "сапог", "берц", "boots")),
    ("shoes", "footwear", ("туфл", "лофер", "мокасин", "shoes")),
    ("sandals", "footwear", ("сандал", "босонож", "sandals")),
    ("slippers", "footwear", ("тапоч", "сланц", "шлепан", "slippers")),
    ("tshirt", "apparel_top", ("футболк", "майк", "t-shirt", "tshirt")),
    ("shirt", "apparel_top", ("рубаш", "блуз", "shirt", "blouse")),
    ("hoodie", "apparel_top", ("худи", "толстов", "свитшот", "hoodie", "sweatshirt")),
    ("sweater", "apparel_top", ("свитер", "джемпер", "пуловер", "кардиган", "sweater")),
    ("jacket", "outerwear", ("куртк", "пуховик", "ветровк", "парк", "jacket")),
    ("coat", "outerwear", ("пальто", "тренч", "coat")),
    ("dress", "apparel_dress", ("плать", "сарафан", "dress")),
    ("skirt", "apparel_bottom", ("юбк", "skirt")),
    ("jeans", "apparel_bottom", ("джинс", "denim jeans", "jeans")),
    ("pants", "apparel_bottom", ("брюк", "штан", "джоггер", "леггинс", "pants", "trousers")),
    ("shorts", "apparel_bottom", ("шорт", "shorts")),
    ("underwear", "underwear", ("трус", "боксер", "нижн бель", "underwear", "boxers")),
    ("socks", "underwear", ("носк", "гольф", "socks")),
    ("backpack", "bags", ("рюкзак", "backpack")),
    ("bag", "bags", ("сумк", "клатч", "шоппер", "bag")),
    ("pan", "cookware", ("сковород", "pan")),
    ("pot", "cookware", ("кастрюл", "сотейник", "pot")),
    ("bedding", "home_textile", ("постельн бель", "пододеяльник", "простын", "bedding")),
    ("towel", "home_textile", ("полотенц", "towel")),
    ("stroller", "baby", ("детск коляск", "прогулочн коляск", "stroller")),
    ("diapers", "baby", ("подгузник", "трусики подгуз", "diapers")),
    ("bicycle", "sport", ("велосипед", "bicycle", "bike")),
    ("fitness_weight", "sport", ("гантел", "гиря", "штанг", "dumbbell")),
    ("drill", "tools", ("дрел", "шуруповерт", "перфоратор", "drill")),
    ("lamp", "lighting", ("светильник", "настольн ламп", "люстр", "lamp")),
    ("mattress", "furniture", ("матрас", "mattress")),
    ("pet_food", "pet", ("корм для кош", "корм для собак", "pet food")),
    ("book", "books", ("книга", "роман", "учебник", "book")),
    ("perfume", "beauty", ("парфюм", "духи", "туалетн вод", "perfume")),
    ("cosmetics", "beauty", ("крем", "сыворотк", "тушь", "помад", "космет", "cosmetic")),
    ("supplement", "health", ("бад", "витамин", "хлорофилл", "капсул", "supplement")),
    ("toy", "toys", ("игруш", "кукл", "конструктор", "toy")),
    ("chair", "furniture", ("стул", "кресл", "chair")),
    ("table", "furniture", ("стол", "table")),
)

TYPE_QUERY_TERMS = {
    "phone_case": "чехол", "screen_protector": "защитное стекло", "charger": "зарядное устройство",
    "cable": "кабель", "smartphone": "смартфон", "smart_speaker": "умная колонка",
    "headphones": "наушники", "speaker": "колонка", "laptop": "ноутбук", "tablet": "планшет",
    "smartwatch": "умные часы", "sneakers": "кроссовки", "boots": "ботинки", "shoes": "туфли",
    "television": "телевизор", "monitor": "монитор", "keyboard": "клавиатура",
    "computer_mouse": "компьютерная мышь", "vacuum": "пылесос", "washing_machine": "стиральная машина",
    "refrigerator": "холодильник", "microwave": "микроволновая печь", "kettle": "электрочайник",
    "coffee_machine": "кофемашина", "blender": "блендер", "iron": "утюг", "hair_dryer": "фен",
    "sandals": "сандалии", "slippers": "тапочки", "tshirt": "футболка", "shirt": "рубашка",
    "hoodie": "худи", "sweater": "свитер", "jacket": "куртка", "coat": "пальто",
    "dress": "платье", "skirt": "юбка", "jeans": "джинсы", "pants": "брюки",
    "shorts": "шорты", "underwear": "трусы", "socks": "носки", "backpack": "рюкзак",
    "bag": "сумка", "perfume": "парфюм", "cosmetics": "косметика", "supplement": "БАД",
    "pan": "сковорода", "pot": "кастрюля", "bedding": "постельное белье", "towel": "полотенце",
    "stroller": "детская коляска", "diapers": "подгузники", "bicycle": "велосипед",
    "fitness_weight": "гантели", "drill": "шуруповерт", "lamp": "светильник",
    "mattress": "матрас", "pet_food": "корм для животных", "book": "книга",
    "toy": "игрушка", "chair": "кресло", "table": "стол",
}

COLOR_RULES = {
    "black": ("черн", "black", "midnight", "темная ночь"),
    "white": ("белый", "белая", "белое", "белые", "белого", "белую", "white", "starlight"),
    "red": ("красн", "бордов", "red"), "blue": ("син", "navy", "blue"),
    "light_blue": ("голуб", "light blue"), "green": ("зелен", "хаки", "green"),
    "yellow": ("желт", "yellow"), "orange": ("оранж", "orange"),
    "purple": ("фиолет", "лаванд", "purple"), "pink": ("розов", "pink"),
    "gray": ("серый", "серая", "серое", "серые", "графит", "gray", "grey"),
    "brown": ("коричн", "шоколад", "brown"), "beige": ("бежев", "кремов", "beige"),
    "gold": ("золот", "gold"), "silver": ("серебр", "silver"),
    "transparent": ("прозрач", "transparent", "clear"),
    "multicolor": ("разноцвет", "мультиколор", "ассорти", "multicolor"),
}

AUDIENCE_RULES = {
    "male": ("мужск", "для мужчин", " men ", "male"),
    "female": ("женск", "для женщин", " women ", "female"),
    "kids": ("детск", "для детей", "малыш", "kids", "child"),
    "teen": ("подрост", "teen"), "unisex": ("унисекс", "unisex"),
}

MATERIAL_RULES = {
    "leather": ("кожан", "натуральная кожа", "экокожа", "leather"),
    "cotton": ("хлопок", "хлопков", "cotton"), "polyester": ("полиэстер", "polyester"),
    "silicone": ("силикон", "silicone"), "metal": ("металл", "алюмини", "сталь", "metal"),
    "wood": ("дерев", "wood"), "plastic": ("пластик", "plastic"), "glass": ("стекл", "glass"),
}

MODEL_QUALIFIERS = {"pro", "max", "mini", "plus", "ultra", "lite", "se", "air"}
STOPWORDS = {
    "для", "или", "при", "это", "как", "что", "на", "в", "во", "с", "со", "из", "от", "до", "по",
    "и", "а", "но", "the", "and", "with", "for", "new", "original", "sale", "купить", "цена", "товар",
    "ozon", "wildberries", "market", "яндекс", "шт", "цвет", "размер", "набор", "комплект",
}

TEXT_ALIASES = {
    "chehol": "чехол", "chekhol": "чехол", "chehly": "чехлы", "case": "чехол",
    "kozhanyy": "кожаный", "kozhanyi": "кожаный", "leather": "кожаный",
    "krossovki": "кроссовки", "sneakers": "кроссовки", "futbolka": "футболка",
    "smartfon": "смартфон", "naushniki": "наушники",
    "айфон": "iphone", "яндекс": "yandex", "самсунг": "samsung", "сяоми": "xiaomi",
}


@dataclass
class ProductIdentity:
    title: str
    brand: str
    product_type: str = ""
    family: str = ""
    tokens: set[str] = field(default_factory=set)
    search_tokens: set[str] = field(default_factory=set)
    models: set[str] = field(default_factory=set)
    model_numbers: set[str] = field(default_factory=set)
    qualifiers: set[str] = field(default_factory=set)
    colors: set[str] = field(default_factory=set)
    audience: set[str] = field(default_factory=set)
    materials: set[str] = field(default_factory=set)
    quantity: int | None = None


@dataclass
class ProductMatchResult:
    score: int
    accepted: bool
    level: str
    reject_reasons: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    components: dict[str, float] = field(default_factory=dict)


def normalize_text(value: str) -> str:
    value = value or ""
    if "Ð" in value or "Ñ" in value:
        try:
            value = value.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    value = value.lower().replace("ё", "е")
    for source, target in TEXT_ALIASES.items():
        value = re.sub(rf"\b{re.escape(source)}\b", target, value)
    value = re.sub(r"https?://\S+", " ", value)
    value = re.sub(r"[^0-9a-zа-я+\-\s]", " ", value, flags=re.IGNORECASE)
    value = value.replace("-", " ")
    value = re.sub(r"\s+", " ", value).strip()
    return f" {value} "


def tokenize(value: str) -> list[str]:
    result = []
    for raw in normalize_text(value).split():
        for token in re.split(r"[-+]+", raw):
            if token and token not in STOPWORDS and (len(token) >= 2 or token.isdigit()):
                result.append(token)
    return result


def _contains_marker(text: str, marker: str) -> bool:
    marker = normalize_text(marker).strip()
    if not marker:
        return False
    if " " in marker:
        marker_tokens = marker.split()
        text_tokens = text.split()
        return any(
            all(text_tokens[start + offset].startswith(expected) for offset, expected in enumerate(marker_tokens))
            for start in range(len(text_tokens) - len(marker_tokens) + 1)
        )
    return any(token.startswith(marker) for token in text.split())


def _detect_type(text: str) -> tuple[str, str]:
    for product_type, family, markers in TYPE_RULES:
        if any(_contains_marker(text, marker) for marker in markers):
            return product_type, family
    return "", ""


def _detect_values(text: str, rules: dict[str, tuple[str, ...]]) -> set[str]:
    return {name for name, markers in rules.items() if any(_contains_marker(text, marker) for marker in markers)}


def _normalize_brand(value: str) -> str:
    brand = " ".join(tokenize(value))
    return "" if brand in GENERIC_BRANDS else brand


def _extract_models(text: str) -> tuple[set[str], set[str], set[str]]:
    normalized = normalize_text(text)
    models = {
        re.sub(r"[\s-]+", "", match.group(0))
        for match in re.finditer(r"\b(?:[a-z]{1,12}|[а-я]{2,12})[ -]?\d{1,5}[a-zа-я0-9]{0,6}\b", normalized, re.IGNORECASE)
    }
    models |= set(re.findall(r"\b\d{1,5}[a-zа-я]{1,8}\b", normalized, re.IGNORECASE))
    ignored_prefixes = ("размер", "рост", "длина", "ширина", "высота", "вес", "объем")
    models = {model for model in models if not model.startswith(ignored_prefixes)}
    quantities = {m.group(1) for m in re.finditer(r"\b(\d{1,3})\s*(?:шт(?:ук[аи]?)?|пар[аы]?|pcs)\b", normalized)}
    numbers = set(re.findall(r"\b\d{2,5}\b", normalized)) - quantities
    qualifiers = set(tokenize(normalized)) & MODEL_QUALIFIERS
    return {model for model in models if len(model) >= 3}, numbers, qualifiers


def _extract_quantity(text: str) -> int | None:
    match = re.search(r"\b(\d{1,3})\s*(?:шт(?:ук[аи]?)?|пар[аы]?|pcs)\b", normalize_text(text))
    return int(match.group(1)) if match else None


def build_identity(title: str, brand: str = "", category: str = "", description: str = "",
                   attributes: dict[str, Any] | None = None) -> ProductIdentity:
    attribute_text = " ".join(str(value) for value in (attributes or {}).values() if value not in (None, ""))
    identity_text = " ".join(part for part in (title, category, attribute_text) if part)
    search_text = " ".join(part for part in (identity_text, description) if part)
    normalized_identity = normalize_text(identity_text)
    product_type, family = _detect_type(normalized_identity)
    models, numbers, qualifiers = _extract_models(identity_text)
    return ProductIdentity(
        title=title, brand=_normalize_brand(brand), product_type=product_type, family=family,
        tokens=set(tokenize(title)), search_tokens=set(tokenize(search_text)),
        models=models, model_numbers=numbers,
        qualifiers=qualifiers, colors=_detect_values(normalized_identity, COLOR_RULES),
        audience=_detect_values(normalized_identity, AUDIENCE_RULES),
        materials=_detect_values(normalize_text(search_text), MATERIAL_RULES),
        quantity=_extract_quantity(identity_text),
    )


def _overlap_score(source: set[str], candidate: set[str]) -> float:
    return len(source & candidate) / max(1, len(source)) if source and candidate else 0.0


def _audience_conflict(source: set[str], candidate: set[str]) -> bool:
    return bool(source and candidate and "unisex" not in source | candidate and not source & candidate)


def match_identities(source: ProductIdentity, candidate: ProductIdentity,
                     visual_similarity: float | None = None, min_score: int = 58,
                     require_variant_evidence: bool = True,
                     allow_missing_evidence: bool = False) -> ProductMatchResult:
    rejects: list[str] = []
    evidence: list[str] = []
    components: dict[str, float] = {}

    if source.family and candidate.family and source.family != candidate.family:
        rejects.append(f"несовместимый тип: {source.family} != {candidate.family}")
    elif source.product_type and candidate.product_type:
        compatible = {source.product_type, candidate.product_type} <= {"jeans", "pants"}
        if source.product_type != candidate.product_type and not compatible:
            rejects.append(f"другой вид товара: {source.product_type} != {candidate.product_type}")
    shared_model = bool(source.models & candidate.models or source.model_numbers & candidate.model_numbers)
    if source.product_type and not candidate.product_type and not shared_model and not allow_missing_evidence:
        rejects.append("тип товара кандидата не подтвержден")
    if source.brand and candidate.brand and source.brand != candidate.brand:
        rejects.append(f"другой бренд: {source.brand} != {candidate.brand}")
    candidate_text = normalize_text(candidate.title)
    if source.brand and not candidate.brand and source.brand not in candidate_text and not allow_missing_evidence:
        rejects.append("бренд кандидата не подтвержден")

    electronics = source.family in {"phone", "phone_accessory", "audio", "computer", "wearable"}
    if source.models and candidate.models and not source.models & candidate.models:
        rejects.append("не совпадает модель")
    elif electronics and source.model_numbers and candidate.model_numbers and not source.model_numbers & candidate.model_numbers:
        rejects.append("не совпадает номер модели")
    if electronics and source.qualifiers != candidate.qualifiers and source.model_numbers & candidate.model_numbers:
        if source.qualifiers or candidate.qualifiers:
            rejects.append("не совпадает модификация модели")
    if source.colors and candidate.colors and "multicolor" not in source.colors | candidate.colors:
        if not source.colors & candidate.colors:
            rejects.append("не совпадает цвет")
    if _audience_conflict(source.audience, candidate.audience):
        rejects.append("не совпадает аудитория")
    if source.quantity and candidate.quantity and source.quantity != candidate.quantity:
        rejects.append("не совпадает комплектность")
    if require_variant_evidence:
        if source.models and not candidate.models:
            rejects.append("модель кандидата не подтверждена")
        elif electronics and source.model_numbers and not candidate.model_numbers:
            rejects.append("номер модели кандидата не подтвержден")
        if source.colors and not candidate.colors and (visual_similarity or 0) < 0.9:
            rejects.append("цвет кандидата не подтвержден")
        apparel_families = {"footwear", "apparel_top", "apparel_bottom", "apparel_dress", "outerwear", "underwear"}
        if source.family in apparel_families and source.audience and not candidate.audience:
            rejects.append("аудитория кандидата не подтверждена")
        if source.quantity and candidate.quantity is None:
            rejects.append("комплектность кандидата не подтверждена")
    if rejects:
        return ProductMatchResult(0, False, "отклонен", rejects, evidence, components)

    if source.product_type and candidate.product_type:
        components["type"] = 25 if source.product_type == candidate.product_type else 19
        evidence.append("тип товара совпадает")
    elif source.family and source.family == candidate.family:
        components["type"] = 17
        evidence.append("категория совпадает")
    else:
        components["type"] = 4

    if source.brand:
        components["brand"] = 18 if candidate.brand == source.brand or source.brand in candidate_text else 3
        if components["brand"] == 18:
            evidence.append("бренд совпадает")
    else:
        components["brand"] = 5

    if source.models and candidate.models:
        components["model"] = 22 * _overlap_score(source.models, candidate.models)
        if source.models & candidate.models:
            evidence.append("модель совпадает")
    elif source.model_numbers and candidate.model_numbers:
        components["model"] = 16 * _overlap_score(source.model_numbers, candidate.model_numbers)
        if source.model_numbers & candidate.model_numbers:
            evidence.append("номер модели совпадает")
    else:
        components["model"] = 8 if not source.models and not source.model_numbers else 2

    components["color"] = 10 if source.colors and candidate.colors else 4 if not source.colors else 1
    components["audience"] = 7 if source.audience and candidate.audience else 3 if not source.audience else 1
    if source.materials and candidate.materials:
        components["material"] = 5 * _overlap_score(source.materials, candidate.materials)
    elif not source.materials:
        components["material"] = 2
    components["quantity"] = 5 if source.quantity and candidate.quantity else 2 if not source.quantity else 0

    title_coverage = _overlap_score(source.tokens, candidate.tokens)
    components["title"] = title_coverage * 20
    if title_coverage >= 0.6:
        evidence.append("название хорошо совпадает")
    if visual_similarity is not None and visual_similarity >= 0.86:
        components["image"] = min(10, (visual_similarity - 0.7) * 33)
        evidence.append(f"фото похоже на {round(visual_similarity * 100)}%")

    score = max(0, min(100, round(sum(components.values()))))
    if score >= 84 and (not source.brand or components.get("brand") == 18) and (
        not source.models or components.get("model", 0) >= 16
    ):
        level = "вероятно тот же товар"
    elif score >= 70:
        level = "очень близкий вариант"
    elif score >= min_score:
        level = "близкий аналог"
    else:
        level = "недостаточное совпадение"
    identity_rejects = []
    if (not allow_missing_evidence and not source.product_type and not candidate.product_type
            and not shared_model and title_coverage < 0.65):
        identity_rejects.append("категория товара не подтверждена")
    if (not allow_missing_evidence and not source.models and not source.model_numbers
            and title_coverage < 0.45 and (visual_similarity or 0) < 0.9):
        identity_rejects.append("недостаточно признаков идентичности")
    rejects.extend(identity_rejects)
    if score < min_score:
        rejects.append(f"недостаточный итоговый score: {score}%")
    accepted = score >= min_score and not identity_rejects
    if not accepted and score >= min_score:
        level = "недостаточное совпадение"
    return ProductMatchResult(score, accepted, level, rejects, evidence[:6], components)


def build_precise_query(identity: ProductIdentity) -> str:
    selected: list[str] = []
    ordered = tokenize(identity.brand)
    ordered += tokenize(TYPE_QUERY_TERMS.get(identity.product_type, ""))
    ordered += sorted(identity.models) + sorted(identity.model_numbers) + sorted(identity.qualifiers)
    ordered += sorted(identity.materials) + sorted(identity.colors) + sorted(identity.audience - {"unisex"})
    ordered += sorted(identity.search_tokens)
    for token in ordered:
        if token and token not in selected:
            selected.append(token)
        if len(selected) >= 11:
            break
    return " ".join(selected)
