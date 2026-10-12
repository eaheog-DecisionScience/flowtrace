class Session:
    def get(self, url):
        return ("session", url)


class Cache:
    def get(self, key):
        return ("cache", key)


def handle(obj):
    return obj.get("x")  # called with two different types below - must NOT be proven either way


def main():
    handle(Session())
    handle(Cache())
