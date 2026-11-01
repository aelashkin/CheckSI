import re
 

_SYMBOL_REPLACEMENTS = {
    '⊕': '+',
    '•+': '+',
    '': '-',
    '': '+',
    '+.': '+ ',
    '•': '',
    'ꞏ': '',
    '–': '-',
    '-': '-',
    '−.': '- ',
    '−': '-',
    '—': '-',
    '―': '-',
    '˗': '-',
    '-.': '- ',
}
# Ordered replacements, shared with callers that must map offsets back.
DASH_REPLACEMENTS = (
    (re.compile(r'•\+'), '+'),
    (re.compile(r'[—–]'), '-'),
    (re.compile(r'•-'), '-'),
    (re.compile(r'·\+'), '+'),
    (re.compile(r'·-'), '-'),
    (re.compile('|'.join(map(re.escape, sorted(_SYMBOL_REPLACEMENTS, key=len, reverse=True)))),
     lambda match: _SYMBOL_REPLACEMENTS[match.group()]),
)


def replace_dash(text):
    # Replace all non-standard characters
    for pattern, replacement in DASH_REPLACEMENTS:
        text = pattern.sub(replacement, text)
    return text


def replace_with_offsets(text, replacements, offsets=None):
    """Apply ordered regex replacements, tracking the source offset of each character.

    ``offsets`` holds one source offset per character plus the end offset.
    Replacement characters take the offset at which their match starts.
    """
    offsets = list(range(len(text) + 1)) if offsets is None else list(offsets)
    for pattern, replacement in replacements:
        pieces, mapped, last = [], [], 0
        for match in pattern.finditer(text):
            new = replacement(match) if callable(replacement) else replacement
            pieces += [text[last:match.start()], new]
            mapped += offsets[last:match.start()] + [offsets[match.start()]] * len(new)
            last = match.end()
        pieces.append(text[last:])
        text, offsets = ''.join(pieces), mapped + offsets[last:]
    return text, offsets


def balance_brackets_in_text(text):
    """ Removing unmatched parentheses in the text """
    stack = []  # A stack for tracking opening parentheses
    extra_indices = set()  # A set for storing the indices of unnecessary parentheses.

    #Checking each character in the text
    for i, char in enumerate(text):
        if char in "({[":  
            stack.append((i, char))  # Remembering the index and type of the parenthesis
        elif char in ")}]": 
            if stack:
                last_index, last_char = stack[-1]
                # Checking if the closing parenthesis matches the opening one.
                if (last_char == "(" and char == ")") or \
                   (last_char == "{" and char == "}") or \
                   (last_char == "[" and char == "]"):
                    stack.pop()  # Found a pair
                else:
                    extra_indices.add(i)  # Extra closing parenthesis
            else:
                extra_indices.add(i)  # Extra closing parenthesis

    # All remaining indices in the stack are unnecessary opening parentheses.
    extra_indices.update(index for index, _ in stack)

    # Gathering the text without unnecessary parentheses
    balanced_text = "".join(
        char for i, char in enumerate(text) if i not in extra_indices
    )
    return balanced_text