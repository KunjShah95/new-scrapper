import os

base = r"C:\ideabot internships\new scrapper\docs"

for f in ["index.html", "report.html"]:
    p = os.path.join(base, f)
    s = open(p, encoding="utf-8").read()
    print(f, len(s), "bytes | title:", s[:55].replace("\n", " "))

p = os.path.join(base, "18946_ETERNITY_BY_RATNAAKAR", "index.html")
s = open(p, encoding="utf-8").read()
print("project page:", len(s), "bytes")
print("pdf links:", s.count("../18946_ETERNITY_BY_RATNAAKAR/files/"))
print("facts cards:", s.count('<div class="fact"'))
print("raw sections:", s.count('<details class="raw"'))
# landing page checks
s = open(base + r"\index.html", encoding="utf-8").read()
print("landing cards:", s.count('<a class="card"'))
print("search input:", 'id="search"' in s)
print("host note:", "GitHub Pages" in s)
print("report link:", 'href="report.html"' in s)

# documents table header check
p = base + r"\18946_ETERNITY_BY_RATNAAKAR\index.html"
s = open(p, encoding="utf-8").read()
i = s.find("2. Documents")
print("\ndocuments table:", s[i:i+300].replace("\n", " "))

print("first h1:", s.split("<h1>")[1].split("</h1>")[0])
print("has footer:", "Generated" in s)
