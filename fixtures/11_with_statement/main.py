class Session:
    def request(self, method, url):
        return {"method": method, "url": url}


def get(url):
    with Session() as session:
        return session.request("GET", url)
