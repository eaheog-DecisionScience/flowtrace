class Session:
    def get(self, url):
        return url


def main():
    s = Session()
    client = s
    return client.get("http://example.com")
