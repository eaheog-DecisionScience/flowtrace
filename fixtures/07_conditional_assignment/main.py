class Session:
    def get(self, url):
        return ("session", url)


class RedisClient:
    def get(self, key):
        return ("redis", key)


def main(condition):
    if condition:
        s = Session()
    else:
        s = RedisClient()
    return s.get("x")  # ambiguous: must be POSSIBLE (both types have .get), never silently resolved to one
