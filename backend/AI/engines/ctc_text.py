from __future__ import annotations

import re
import unicodedata

_NUMBER_RE = re.compile(r"\d+")
_NUMBER_CONFIG = {
    "Russian": {
        "small": tuple("ноль один два три четыре пять шесть семь восемь девять десять одиннадцать двенадцать тринадцать четырнадцать пятнадцать шестнадцать семнадцать восемнадцать девятнадцать".split()),  # noqa: SIM905
        "tens": ("", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто"),
        "hundreds": ("", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот"),
        "feminine": {1: "одна", 2: "две"},
        "scales": (
            (10**12, ("триллион", "триллиона", "триллионов"), False),
            (10**9, ("миллиард", "миллиарда", "миллиардов"), False),
            (10**6, ("миллион", "миллиона", "миллионов"), False),
            (10**3, ("тысяча", "тысячи", "тысяч"), True),
        ),
    },
    "Ukrainian": {
        "small": tuple("нуль один два три чотири п'ять шість сім вісім дев'ять десять одинадцять дванадцять тринадцять чотирнадцять п'ятнадцять шістнадцять сімнадцять вісімнадцять дев'ятнадцять".split()),  # noqa: SIM905
        "tens": ("", "", "двадцять", "тридцять", "сорок", "п'ятдесят", "шістдесят", "сімдесят", "вісімдесят", "дев'яносто"),
        "hundreds": ("", "сто", "двісті", "триста", "чотириста", "п'ятсот", "шістсот", "сімсот", "вісімсот", "дев'ятсот"),
        "feminine": {1: "одна", 2: "дві"},
        "scales": (
            (10**12, ("трильйон", "трильйони", "трильйонів"), False),
            (10**9, ("мільярд", "мільярди", "мільярдів"), False),
            (10**6, ("мільйон", "мільйони", "мільйонів"), False),
            (10**3, ("тисяча", "тисячі", "тисяч"), True),
        ),
    },
    "English": {
        "small": tuple("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()),  # noqa: SIM905
        "tens": ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"),
        "scales": ((10**12, "trillion"), (10**9, "billion"), (10**6, "million"), (10**3, "thousand")),
    },
}


def _plural_form(value: int, forms: tuple[str, str, str]) -> str:
    last_two = value % 100
    if 11 <= last_two <= 14:
        return forms[2]
    last = value % 10
    return forms[0] if last == 1 else forms[1] if 2 <= last <= 4 else forms[2]


def _triplet_words(value: int, language: str, feminine: bool = False) -> list[str]:
    config = _NUMBER_CONFIG[language]
    if language == "English":
        result = []
        if value >= 100:
            result.extend((config["small"][value // 100], "hundred"))
            value %= 100
        if value:
            if value < 20:
                result.append(config["small"][value])
            else:
                result.append(config["tens"][value // 10])
                if value % 10:
                    result.append(config["small"][value % 10])
        return result
    result = []
    if value >= 100:
        result.append(config["hundreds"][value // 100])
        value %= 100
    if not value:
        return result
    if value < 20:
        result.append(config["feminine"].get(value, config["small"][value]) if feminine else config["small"][value])
        return result
    result.append(config["tens"][value // 10])
    unit = value % 10
    if unit:
        result.append(config["feminine"].get(unit, config["small"][unit]) if feminine else config["small"][unit])
    return result


def _integer_words(value: int, language: str) -> str:
    language = language if language in _NUMBER_CONFIG else "English"
    config = _NUMBER_CONFIG[language]
    if value == 0:
        return config["small"][0]
    if value >= 10**15:
        return "".join(config["small"][int(digit)] for digit in str(value))
    result = []
    if language == "English":
        for scale, name in config["scales"]:
            group, value = divmod(value, scale)
            if group:
                result.extend(_triplet_words(group, language))
                result.append(name)
    else:
        for scale, forms, feminine in config["scales"]:
            group, value = divmod(value, scale)
            if group:
                result.extend(_triplet_words(group, language, feminine))
                result.append(_plural_form(group, forms))
    result.extend(_triplet_words(value, language))
    return "".join(result)


def ctc_token(token: str, language: str) -> str:
    normalized = unicodedata.normalize("NFKC", token).casefold()

    def replace(match: re.Match[str]) -> str:
        digits = match.group(0)
        if len(digits) > 1 and digits.startswith("0"):
            return "".join(_integer_words(int(digit), language) for digit in digits)
        return _integer_words(int(digits), language)

    normalized = _NUMBER_RE.sub(replace, normalized)
    return "".join(char for char in normalized if char == "'" or unicodedata.category(char)[:1] == "L")


def ctc_tokens(tokens: list[str], language: str) -> list[str]:
    return [ctc_token(token, language) or token for token in tokens]


def ctc_language_encodable(token: str, language: str) -> bool:
    normalized = ctc_token(token, language)
    if language not in {"Russian", "Ukrainian"}:
        return bool(normalized)
    return bool(normalized) and all(
        character == "'" or "CYRILLIC" in unicodedata.name(character, "")
        for character in normalized
    )


def create_ctc_aligner(model_path: str, language: str):
    from .ctc import CTCWordAligner

    aligner = CTCWordAligner(model_path)
    aligner.role = "ctc_uk" if language == "Ukrainian" else "ctc_ru"
    return aligner
