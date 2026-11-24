"""Deterministic, keyed, same-length pseudonyms.

Every identifying word maps to one pseudonym word of the same length: letters stay
letters (case kept), digits stay digits.
The mapping is HMAC-SHA256(secret key, lowercased word), so the same identity gets the
same pseudonym in every file on every machine with no shared state, and nothing can be
reversed without the key. Same length keeps length-prefixed and fixed-size fields in
CAD files valid.
"""
import hashlib
import hmac
import os
import re

_LETTERS = "abcdefghijklmnopqrstuvwxyz"
_DIGITS = "0123456789"
_WORD = re.compile(r"[A-Za-z0-9]{2,}")
_WIDE = re.compile(r"[^\W\d_]*[^\x00-\x7f\W][^\W\d_]*")  # a letter run with a non-ASCII letter
# Words that say what kind of body a name belongs to, not who it is. They stay readable
# inside a pseudonymized name ("University of Munich" -> "University of Fmsoha") unless
# the name has nothing else.
KEEP = set("""
university universite universitat universidad universita college school institute institut
instituto laboratory laboratories lab labs department dept faculty center centre
of the and for in at de la le du des der und et di del van von
inc incorporated ltd limited llc gmbh corp corporation company co ag bv kg sa srl spa pty
plc oy ab nv group foundation association society technology technologies engineering
systems solutions industries international national research
www http https com org net edu gov io fr uk it ch eu nl es
""".split())


def load_key():
    key = os.environ.get("DEID_KEY", "")
    key_file = os.environ.get("DEID_KEY_FILE", "")
    if not key and key_file:
        with open(key_file, encoding="utf-8") as f:
            key = f.read().strip()
    if len(key) < 16:
        raise SystemExit("Set DEID_KEY or DEID_KEY_FILE to a private secret of 16+ characters.")
    return key.encode("utf-8")


class Pseudo:
    def __init__(self, key):
        self.key = key
        self.cache = {}

    def reserve(self, words):
        """Give every word of a known list its own pseudonym: if two words would get the
        same one, the later word (in sorted order) takes its next candidate. Every
        machine reserves the same list, so they still agree. Returns the collisions fixed."""
        used, fixed = {}, 0
        # numbers and two-letter words have too few pseudonyms to all be different
        known = {w.lower() for w in words if len(w) >= 3 and w.isascii() and w.isalnum() and not w.isdigit()}
        for lw in sorted(known):
            out = self._make(lw)
            salt = 16
            while out in used and salt < 1024:
                out = self._make(lw, salt)
                salt += 16
            if out in used:
                raise SystemExit("pseudonym collision that could not be resolved: %r" % lw)
            fixed += salt > 16
            used[out] = lw
            self.cache[lw] = out
        return fixed

    def _make(self, lw, first_salt=0):
        out = lw
        for salt in range(first_salt, first_salt + 16):
            seed = hmac.new(self.key, b"%d:" % salt + lw.encode("ascii"), hashlib.sha256).digest()
            stream = hashlib.shake_256(seed).digest(len(lw))
            chars = []
            for ch, r in zip(lw, stream):
                # any letter for a letter: the larger space keeps different names different
                chars.append(_DIGITS[r % 10] if ch in _DIGITS else _LETTERS[r % 26])
            out = "".join(chars)
            if out != lw:
                break
        return out

    def word(self, w):
        """Pseudonym for one ASCII alphanumeric word (same length, same case pattern)."""
        lw = w.lower()
        base = self.cache.get(lw)
        if base is None:
            base = self._make(lw)
            if len(self.cache) < 500000:
                self.cache[lw] = base
        return "".join(b.upper() if c.isupper() else b for b, c in zip(base, w))

    def span(self, text):
        """Pseudonymize the ASCII alphanumeric words (2+ chars) of an identity string;
        generic body words (KEEP) stay when the name has at least one other word."""
        words = _WORD.findall(text)
        if len(words) > 1 and any(w.lower() in KEEP for w in words) and not all(w.lower() in KEEP for w in words):
            return _WORD.sub(lambda m: m.group() if m.group().lower() in KEEP else self.word(m.group()), text)
        return _WORD.sub(lambda m: self.word(m.group()), text)

    def span_all(self, text):
        """span(), and also letter runs with non-ASCII letters ("冠禎", "Müller"): each becomes
        a keyed run of as many characters (CJK for CJK, ASCII letters otherwise). For text
        fields only (PDF metadata), where the byte length does not matter."""
        def wide(m):
            run = m.group()
            seed = hashlib.shake_256(hmac.new(self.key, b"w:" + run.lower().encode("utf-8"),
                                               hashlib.sha256).digest()).digest(len(run))
            return "".join(chr(0x4E00 + (r * 83) % 0x5000) if 0x3400 <= ord(c) <= 0x9FFF
                           else (_LETTERS[r % 26].upper() if c.isupper() else _LETTERS[r % 26])
                           for c, r in zip(run, seed))
        return _WIDE.sub(wide, self.span(text))
