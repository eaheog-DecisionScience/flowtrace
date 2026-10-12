class Session:
    def get(self, url):
        return self.request("GET", url)

    def request(self, method, url):
        return {"method": method, "url": url}


def main():
    session = Session()
    return session.get("http://example.com")
