class Session:
    def get(self, url):
        return url


class Other:
    def get(self, url):
        return url


def outer():
    s = Other()  # outer scope: s is Other

    def inner():
        s = Session()  # inner scope: independent s, is Session
        return s.get("http://example.com")  # should resolve to Session.get, not Other.get

    return inner()
