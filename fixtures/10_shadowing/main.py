class Session:
    def get(self, url):
        return url


class Cache:
    def get(self, key):
        return key


def main():
    s = Session()
    s.get("http://example.com")  # a flow-sensitive analyzer would know only Session is live here
    s = Cache()
    return s.get("key")  # ...and only Cache here. Phase 1 is NOT flow-order-sensitive (see inference.py's
    # docstring) - it unions every type ever assigned to `s` across the whole function, so BOTH call
    # sites are treated as ambiguous (possible: Session.get and Cache.get) rather than resolved. This is
    # the documented, deliberate conservative tradeoff: less resolved, but never wrong.
