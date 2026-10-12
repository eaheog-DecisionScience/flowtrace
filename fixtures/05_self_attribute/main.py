class Session:
    def get(self, url):
        return url


class Client:
    def __init__(self):
        self.session = Session()

    def request(self):
        return self.session.get("http://example.com")
