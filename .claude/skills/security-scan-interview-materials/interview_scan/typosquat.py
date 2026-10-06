def edit_distance(a: str, b: str, give_up_above: int) -> int:
    if abs(len(a) - len(b)) > give_up_above:
        return give_up_above + 1
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        for j, char_b in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b)))
        if min(current) > give_up_above:
            return give_up_above + 1
        previous = current
    return previous[-1]


def allowed_distance_for(popular_name: str) -> int:
    if len(popular_name) < 5:
        return 0
    return 1 if len(popular_name) < 8 else 2


def find_impersonated_package(name: str, popular: set[str], suffixes: list[str]) -> str | None:
    lowered = name.lower()
    if lowered in popular or lowered.startswith("@"):
        return None
    for candidate in popular:
        allowed = allowed_distance_for(candidate)
        if allowed and 0 < edit_distance(lowered, candidate, allowed) <= allowed:
            return candidate
        if any(lowered == candidate + suffix for suffix in suffixes):
            return candidate
    return None
