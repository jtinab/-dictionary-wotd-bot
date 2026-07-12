"""
Dictionary.com Word of the Day -> Telegram
--------------------------------------------
Fetches the real, current Word of the Day from dictionary.com (word,
phonetic spelling, part of speech, explanation, and example sentence)
and sends it to you on Telegram.

This uses a single plain HTTP request to a public page - no login, no
browser automation, no clicking anything. Much lower risk of being
blocked than a login-gated site, but if dictionary.com ever changes
their page layout, the parsing step below may need small adjustments
(the debug dump makes that easy to diagnose - see README).
"""

import os
import re
import sys
import json
import logging
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

WOTD_URL = "https://www.dictionary.com/word-of-the-day"
STATE_FILE = Path(__file__).parent / "last_sent.json"
DEBUG_DIR = Path(__file__).parent / "debug"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("dotd_bot")

DATE_PATTERN = re.compile(r"^[A-Z][a-z]+ \d{1,2}, \d{4}$")
PARTS_OF_SPEECH = {"noun", "verb", "adjective", "adverb", "pronoun",
                    "preposition", "conjunction", "interjection", "phrase"}


def send_telegram_message(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured. Message would have been:\n%s", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"})
    if resp.status_code != 200:
        log.error("Failed to send Telegram message: %s", resp.text)
    else:
        log.info("Telegram message sent.")


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def debug_dump(text: str) -> None:
    DEBUG_DIR.mkdir(exist_ok=True)
    (DEBUG_DIR / "page_text.txt").write_text(text)


def fix_spacing(text: str) -> str:
    """
    Dictionary.com bolds words inline (e.g. the word itself, mid-sentence).
    BeautifulSoup's line-based text extraction inserts a line break at every
    tag boundary, which - once lines are rejoined with spaces - leaves stray
    spaces before punctuation (e.g. "au fond , let's"). This cleans that up.
    """
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([\[(])\s+", r"\1", text)
    text = re.sub(r"\s+([\])])", r"\1", text)
    return text.strip()


def fetch_word_of_the_day():
    """
    Returns a dict with date, word, phonetic, part_of_speech, definition,
    explanation, example - or None if parsing failed.
    """
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"}
    resp = requests.get(WOTD_URL, headers=headers, timeout=15)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    full_text = soup.get_text("\n")
    lines = [l.strip() for l in full_text.split("\n") if l.strip()]

    debug_dump("\n".join(lines))  # always save, useful if parsing ever breaks

    # Find the first date line - that marks the start of today's entry
    date_indices = [i for i, l in enumerate(lines) if DATE_PATTERN.match(l)]
    if not date_indices:
        log.error("Could not find a date entry on the page - layout may have changed.")
        return None

    start = date_indices[0]
    end = date_indices[1] if len(date_indices) > 1 else len(lines)
    block = lines[start:end]
    date_str = block[0]

    # Join everything after the date into one blob and fix spacing artifacts
    # caused by inline bold tags splitting text across "lines".
    body = fix_spacing(" ".join(block[1:]))

    # Word: everything before the first "[" (the phonetic spelling starts there)
    phon_match = re.search(r"\[([^\]]+)\]", body)
    if not phon_match:
        log.error("Could not find a phonetic spelling (in brackets) - layout may have changed.")
        return None

    word = body[:phon_match.start()].strip()
    phonetic = f"[{phon_match.group(1).strip()}]"
    remainder = body[phon_match.end():].strip()

    # Part of speech: the word right after the phonetic spelling, if it's
    # one we recognize (ADVERB, VERB, etc. - dictionary.com uppercases it)
    part_of_speech = ""
    pos_match = re.match(r"^([A-Za-z]+)\b", remainder)
    if pos_match and pos_match.group(1).lower() in PARTS_OF_SPEECH:
        part_of_speech = pos_match.group(1).lower()
        remainder = remainder[pos_match.end():].strip()

    # Definition: everything up to the "EXPLANATION" marker
    parts = re.split(r"\bEXPLANATION\b", remainder, maxsplit=1, flags=re.IGNORECASE)
    definition = parts[0].strip()
    rest = parts[1].strip() if len(parts) > 1 else ""

    # Explanation: everything up to the "EXAMPLE" marker
    parts = re.split(r"\bEXAMPLE\b", rest, maxsplit=1, flags=re.IGNORECASE)
    explanation = parts[0].strip()
    example = parts[1].strip() if len(parts) > 1 else ""
    example = example.split("Get the Word of the Day")[0].strip()

    if not word or not definition:
        log.error("Parsing produced an empty word or definition - layout may have changed.")
        return None

    return {
        "date": date_str,
        "word": word,
        "phonetic": phonetic,
        "part_of_speech": part_of_speech,
        "definition": definition,
        "explanation": explanation,
        "example": example,
    }


def main():
    entry = fetch_word_of_the_day()
    if not entry:
        log.error("Could not fetch/parse today's word. Check debug/page_text.txt.")
        sys.exit(1)

    state = load_state()
    if state.get("last_word") == entry["word"] and state.get("last_date") == entry["date"]:
        log.info("Already sent today's word (%s) - skipping duplicate send.", entry["word"])
        return

    lines = [f"📖 <b>Word of the Day: {entry['word'].capitalize()}</b>"]
    if entry["phonetic"]:
        lines.append(f"<i>{entry['phonetic']}</i>")
    if entry["part_of_speech"]:
        lines.append(f"\n<b>{entry['part_of_speech'].upper()}</b>")
    lines.append(entry["definition"])
    if entry["explanation"]:
        lines.append(f"\n<b>Explanation:</b> {entry['explanation']}")
    if entry["example"]:
        lines.append(f"\n<b>Example:</b> \"{entry['example']}\"")

    message = "\n".join(lines)
    log.info("Sending word: %s (%s)", entry["word"], entry["date"])
    send_telegram_message(message)

    save_state({"last_word": entry["word"], "last_date": entry["date"]})


if __name__ == "__main__":
    main()

DATE_PATTERN = re.compile(r"^[A-Z][a-z]+ \d{1,2}, \d{4}$")
PARTS_OF_SPEECH = {"noun", "verb", "adjective", "adverb", "pronoun",
                    "preposition", "conjunction", "interjection", "phrase"}


def send_telegram_message(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured. Message would have been:\n%s", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"})
    if resp.status_code != 200:
        log.error("Failed to send Telegram message: %s", resp.text)
    else:
        log.info("Telegram message sent.")


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def debug_dump(text: str) -> None:
    DEBUG_DIR.mkdir(exist_ok=True)
    (DEBUG_DIR / "page_text.txt").write_text(text)


def fix_spacing(text: str) -> str:
    """
    Dictionary.com bolds words inline (e.g. the word itself, mid-sentence).
    BeautifulSoup's line-based text extraction inserts a line break at every
    tag boundary, which - once lines are rejoined with spaces - leaves stray
    spaces before punctuation (e.g. "au fond , let's"). This cleans that up.
    """
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([\[(])\s+", r"\1", text)
    text = re.sub(r"\s+([\])])", r"\1", text)
    return text.strip()


def fetch_word_of_the_day():
    """
    Returns a dict with date, word, phonetic, part_of_speech, definition,
    explanation, example - or None if parsing failed.
    """
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"}
    resp = requests.get(WOTD_URL, headers=headers, timeout=15)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    full_text = soup.get_text("\n")
    lines = [l.strip() for l in full_text.split("\n") if l.strip()]

    debug_dump("\n".join(lines))  # always save, useful if parsing ever breaks

    # Find the first date line - that marks the start of today's entry
    date_indices = [i for i, l in enumerate(lines) if DATE_PATTERN.match(l)]
    if not date_indices:
        log.error("Could not find a date entry on the page - layout may have changed.")
        return None

    start = date_indices[0]
    end = date_indices[1] if len(date_indices) > 1 else len(lines)
    block = lines[start:end]
    date_str = block[0]

    # Join everything after the date into one blob and fix spacing artifacts
    # caused by inline bold tags splitting text across "lines".
    body = fix_spacing(" ".join(block[1:]))

    # Word: everything before the first "[" (the phonetic spelling starts there)
    phon_match = re.search(r"\[([^\]]+)\]", body)
    if not phon_match:
        log.error("Could not find a phonetic spelling (in brackets) - layout may have changed.")
        return None

    word = body[:phon_match.start()].strip()
    phonetic = f"[{phon_match.group(1).strip()}]"
    remainder = body[phon_match.end():].strip()

    # Part of speech: the word right after the phonetic spelling, if it's
    # one we recognize (ADVERB, VERB, etc. - dictionary.com uppercases it)
    part_of_speech = ""
    pos_match = re.match(r"^([A-Za-z]+)\b", remainder)
    if pos_match and pos_match.group(1).lower() in PARTS_OF_SPEECH:
        part_of_speech = pos_match.group(1).lower()
        remainder = remainder[pos_match.end():].strip()

    # Definition: everything up to the "EXPLANATION" marker
    parts = re.split(r"\bEXPLANATION\b", remainder, maxsplit=1, flags=re.IGNORECASE)
    definition = parts[0].strip()
    rest = parts[1].strip() if len(parts) > 1 else ""

    # Explanation: everything up to the "EXAMPLE" marker
    parts = re.split(r"\bEXAMPLE\b", rest, maxsplit=1, flags=re.IGNORECASE)
    explanation = parts[0].strip()
    example = parts[1].strip() if len(parts) > 1 else ""
    example = example.split("Get the Word of the Day")[0].strip()

    if not word or not definition:
        log.error("Parsing produced an empty word or definition - layout may have changed.")
        return None

    return {
        "date": date_str,
        "word": word,
        "phonetic": phonetic,
        "part_of_speech": part_of_speech,
        "definition": definition,
        "explanation": explanation,
        "example": example,
    }


def main():
    entry = fetch_word_of_the_day()
    if not entry:
        log.error("Could not fetch/parse today's word. Check debug/page_text.txt.")
        sys.exit(1)

    state = load_state()
    if state.get("last_word") == entry["word"] and state.get("last_date") == entry["date"]:
        log.info("Already sent today's word (%s) - skipping duplicate send.", entry["word"])
        return

    lines = [f"📖 <b>Word of the Day: {entry['word'].capitalize()}</b>"]
    subtitle_bits = [b for b in [entry["phonetic"], entry["part_of_speech"]] if b]
    if subtitle_bits:
        lines.append(f"<i>{' · '.join(subtitle_bits)}</i>")
    lines.append("")
    lines.append(f"<b>Meaning:</b> {entry['definition']}")
    if entry["explanation"]:
        lines.append(f"\n<b>Explanation:</b> {entry['explanation']}")
    if entry["example"]:
        lines.append(f"\n<b>Example:</b> \"{entry['example']}\"")

    message = "\n".join(lines)
    log.info("Sending word: %s (%s)", entry["word"], entry["date"])
    send_telegram_message(message)

    save_state({"last_word": entry["word"], "last_date": entry["date"]})


if __name__ == "__main__":
    main()
