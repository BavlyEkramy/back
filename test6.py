import pandas as pd
import requests
from bs4 import BeautifulSoup
import re
import time
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from pymongo import MongoClient
import google.generativeai as genai
import certifi
import os

from pinecone import Pinecone, ServerlessSpec
from sentence_transformers import SentenceTransformer

API_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

# === إعداد سيشن مع retry ===
session = requests.Session()
retries = Retry(total=7, backoff_factor=2, status_forcelist=[500, 502, 503, 504])
session.mount("https://", HTTPAdapter(max_retries=retries))

# === MongoDB Atlas Connection ===
MONGO_URI = "mongodb+srv://infocodivera_db_user:m6Uwjdv2f53imWeJ@cluster0.ldqe96m.mongodb.net/?retryWrites=true&w=majority"
# MONGO_URI = "mongodb://localhost:27017/"

try:
    client = MongoClient(
        MONGO_URI, tls=True, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=20000
    )
    db = client["nasa_papers"]
    collection = db["lol"]
    print("✅ Connected to MongoDB Atlas successfully")
except Exception as e:
    print(f"❌ MongoDB connection error: {e}")

# === Gemini API config ===
genai.configure(api_key="AIzaSyAj4iFTHfZpF8oy7k7AyfM5-2VO34SOXqU")
model = genai.GenerativeModel("models/gemini-2.5-flash")

# === Pinecone config ===
pc = Pinecone(
    api_key="pcsk_RT6wY_N5JbiUjPaTaLDaxXepgh7uPXpKj7wmiJVAjARHPc2HzDodSnPRKTRRZpCyEoKzh"
)
INDEX_NAME = "nasa-articles-chunks"

if INDEX_NAME not in pc.list_indexes().names():
    pc.create_index(
        name=INDEX_NAME,
        dimension=384,
        metric="cosine",
        spec=ServerlessSpec(cloud="aws", region="us-east-1"),
    )

index = pc.Index(INDEX_NAME)
embedding_model = SentenceTransformer("all-MiniLM-L6-v2")


# === Gemini Summarizer ===
def generate_summary_and_significant(title, abstract_text):
    if not abstract_text:
        return None, None

    prompt = f"""
    You are a scientific assistant.
    Based on the following research title and abstract:

    Title: {title}
    Abstract: {abstract_text}

    1. Write a concise **Summary** (3–4 sentences).
    2. List the **Significant Findings** (3–5 bullet points).
    """

    response = model.generate_content(prompt)
    text = response.text if response and response.text else ""

    summary, significant = None, None
    if "Significant" in text:
        parts = text.split("Significant")
        summary = parts[0].strip()
        significant = "Significant " + parts[1].strip()
    else:
        summary = text.strip()

    return summary, significant


# === Extract sections from XML ===
def extract_sections(soup, pmc_id=None):
    sections = {}

    # Abstract
    abs_tags = soup.find_all("abstract")
    if abs_tags:
        sections["abstract"] = " ".join(a.get_text(" ", strip=True) for a in abs_tags)

    # Body sections
    for sec in soup.find_all("sec"):
        sec_title = sec.find("title")
        if not sec_title:
            continue
        title_text = sec_title.get_text(" ", strip=True).lower()
        sec_text = sec.get_text(" ", strip=True)

        if "introduction" in title_text:
            sections["introduction"] = sec_text
        elif "method" in title_text:
            sections["methods"] = sec_text
        elif "result" in title_text:
            sections["results"] = sec_text
        elif "discussion" in title_text:
            sections["discussion"] = sec_text
        elif "conclusion" in title_text:
            sections["conclusion"] = sec_text

    # === Figures (text only, ignore links) ===
    figures = []
    for fig in soup.find_all("fig"):
        label = fig.find("label")
        caption = fig.find("caption")

        text = ""
        if label:
            text += label.get_text(" ", strip=True) + " "
        if caption:
            text += caption.get_text(" ", strip=True)

        if text.strip():
            figures.append({"text": text.strip()})

    if figures:
        sections["figures"] = figures

    # === Tables ===
    tables = []
    for tbl in soup.find_all("table-wrap"):
        label = tbl.find("label")
        caption = tbl.find("caption")

        text = ""
        if label:
            text += label.get_text(" ", strip=True) + " "
        if caption:
            text += caption.get_text(" ", strip=True)

        if text.strip():
            tables.append({"text": text.strip()})

    if tables:
        sections["tables"] = tables

    return sections


# === Upsert chunks into Pinecone ===
def upsert_article_chunks(article_doc, sections):
    vectors = []
    pmc_id = article_doc["pmc_id"]

    # Add XML sections
    for sec_name, sec_text in sections.items():
        if isinstance(sec_text, list):
            for i, item in enumerate(sec_text):
                text = item.get("text", "")
                if not text.strip():
                    continue
                meta = {
                    "pmc_id": pmc_id,
                    "section": sec_name,
                    "title": article_doc["title"],
                    "link": article_doc["link"],
                    "text": text,
                }

                vectors.append(
                    {
                        "id": f"{pmc_id}_{sec_name}_{i}",
                        "values": embedding_model.encode(text).tolist(),
                        "metadata": meta,
                    }
                )
        elif isinstance(sec_text, str) and sec_text.strip():
            vectors.append(
                {
                    "id": f"{pmc_id}_{sec_name}",
                    "values": embedding_model.encode(sec_text).tolist(),
                    "metadata": {
                        "pmc_id": pmc_id,
                        "section": sec_name,
                        "title": article_doc["title"],
                        "link": article_doc["link"],
                        "text": sec_text,
                    },
                }
            )

    # Add Gemini summary
    if article_doc.get("summary"):
        vectors.append(
            {
                "id": f"{pmc_id}_summary",
                "values": embedding_model.encode(article_doc["summary"]).tolist(),
                "metadata": {
                    "pmc_id": pmc_id,
                    "section": "summary",
                    "title": article_doc["title"],
                    "link": article_doc["link"],
                    "text": article_doc["summary"],
                },
            }
        )

    # Add Gemini findings (split each point)
    if article_doc.get("significant"):
        findings = [
            f.strip("-• ") for f in article_doc["significant"].split("\n") if f.strip()
        ]
        for i, f in enumerate(findings):
            vectors.append(
                {
                    "id": f"{pmc_id}_finding_{i}",
                    "values": embedding_model.encode(f).tolist(),
                    "metadata": {
                        "pmc_id": pmc_id,
                        "section": "finding",
                        "title": article_doc["title"],
                        "link": article_doc["link"],
                        "text": f,
                    },
                }
            )

    if vectors:
        index.upsert(vectors=vectors)
        print(f"📌 Inserted {len(vectors)} chunks for {pmc_id}")


# === Fetch article & build doc ===
def fetch_article_full(pmc_id, link, title_from_csv):
    params = {"db": "pmc", "id": pmc_id, "rettype": "full", "retmode": "xml"}
    r = session.get(API_URL, params=params, timeout=40)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml-xml")

    title_tag = soup.find("article-title")
    title = title_tag.get_text(" ", strip=True) if title_tag else title_from_csv

    authors = []
    for contrib in soup.find_all("contrib", {"contrib-type": "author"}):
        name_parts = []
        if contrib.find("surname"):
            name_parts.append(contrib.find("surname").get_text())
        if contrib.find("given-names"):
            name_parts.append(contrib.find("given-names").get_text())
        if name_parts:
            authors.append(" ".join(name_parts))

    pub_date = (
        soup.find("pub-date", {"pub-type": "epub"})
        or soup.find("pub-date", {"pub-type": "ppub"})
        or soup.find("pub-date")
    )
    date_str = None
    if pub_date:
        year = pub_date.find("year").get_text() if pub_date.find("year") else ""
        month = pub_date.find("month").get_text() if pub_date.find("month") else ""
        day = pub_date.find("day").get_text() if pub_date.find("day") else ""
        date_str = "-".join(filter(None, [year, month, day]))

    pdf_tag = soup.find("self-uri", {"content-type": "pmc-pdf"})
    if pdf_tag and pdf_tag.has_attr("xlink:href"):
        pdf_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc_id}/pdf/{pdf_tag['xlink:href']}"
    else:
        pdf_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc_id}/pdf/"

    abstracts = [
        abs_tag.get_text(" ", strip=True) for abs_tag in soup.find_all("abstract")
    ]
    abstract_text = "\n\n".join(abstracts) if abstracts else None

    sections = extract_sections(soup, pmc_id)
    summary, significant = generate_summary_and_significant(title, abstract_text)

    article_doc = {
        "pmc_id": pmc_id,
        "link": link,
        "title": title,
        "authors": authors,
        "published_date": date_str,
        "pdf_url": pdf_url,
        "abstract": abstract_text,
        "summary": summary,
        "significant": significant,
        "figures": sections.get("figures", []),  # only text
        "tables": sections.get("tables", []),
    }

    for k, v in sections.items():
        if k not in ["figures", "tables"]:
            article_doc[k] = v

    return article_doc, sections


# === Extract PMC ID ===
def extract_pmc_id(link):
    match = re.search(r"(PMC\d+)", link)
    return match.group(1) if match else None


# === Main ===
# if __name__ == "__main__":
#     df = pd.read_csv("SB_publication_PMC.csv")
#     total = len(df)

#     for idx, row in df.iterrows():
#         title = row["Title"]
#         link = row["Link"]
#         pmc_id = extract_pmc_id(link)

#         if not pmc_id:
#             print(f"❌ Skipping row {idx+1}/{total}, no PMC ID found in {link}")
#             continue

#         try:
#             doc, sections = fetch_article_full(pmc_id, link, title)
#             collection.update_one({"pmc_id": pmc_id}, {"$set": doc}, upsert=True)
#             upsert_article_chunks(doc, sections)  # Pinecone

#             progress = (idx + 1) / total * 100
#             print(f"✅ [{idx+1}/{total}] ({progress:.2f}%) Saved: {title} ({pmc_id})")

#         except Exception as e:
#             print(f"❌ Error with {pmc_id}: {e}")

#         time.sleep(2)
# === Main ===
if __name__ == "__main__":
    df_csv = pd.read_csv("SB_publication_PMC.csv")
    total = len(df_csv)

    OUTPUT_FILE = "processed_articles_data.xlsx"    

    # لو الملف موجود هنقرأه، لو مش موجود نبدأ DataFrame جديد
    if os.path.exists(OUTPUT_FILE):
        df = pd.read_excel(OUTPUT_FILE)
    else:
        df = pd.DataFrame(
            columns=[
                "pmc_id",
                "title",
                "link",
                "authors",
                "published_date",
                "abstract",
                "summary",
                "significant",
                "pdf_url",
                "introduction",
                "results",
                "conclusion",
                "discussion",
                "full_text"
            ]
        )
        df.to_excel(OUTPUT_FILE, index=False, engine="openpyxl")

    for idx, row in df_csv.iterrows():
        title = row["Title"]
        link = row["Link"]
        pmc_id = extract_pmc_id(link)
        if not pmc_id:
            print(f"❌ Skipping row {idx+1}/{total}, no PMC ID found in {link}")
            continue

        # لو الورقة موجودة بالفعل في Excel أو MongoDB نتخطاها
        if pmc_id in df["pmc_id"].values:
            print(f"⏩ Skipping {pmc_id}, already in Excel")
            continue

        try:
            doc, sections = fetch_article_full(pmc_id, link, title)

            # تخزين في MongoDB
            # collection.update_one({"pmc_id": pmc_id}, {"$set": doc}, upsert=True)

            # تخزين في Pinecone
            # upsert_article_chunks(doc, sections)

            # إضافة صف جديد لـ Excel DataFrame

            full_text = doc.get("introduction", "") + doc.get("conclusion", "") + doc.get("results","")
           
            # full_text = " ".join([part for part in text_parts if part])  # اجمع النصوص
            # print(f"full_text {full_text}")
            new_row = {
                "pmc_id": doc["pmc_id"],
                "title": doc["title"],
                "link": doc["link"],
                "authors": ", ".join(doc["authors"]),
                "published_date": doc["published_date"],
                "abstract": doc.get("abstract", ""),
                "summary": doc.get("summary", ""),
                "significant": doc.get("significant", ""),
                "pdf_url": doc["pdf_url"],
                "introduction": doc.get("introduction",""),
                "results": doc.get("results",""),
                "conclusion": doc.get("conclusion",""),
                "discussion": doc.get("discussion",""),   
                "full_text": full_text,   
            }

            df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)

            # حفظ الملف بعد كل عملية (backup فوري)
            df.to_excel(OUTPUT_FILE, index=False, engine="openpyxl")

            progress = (idx + 1) / total * 100
            print(
                f"✅ [{idx+1}/{total}] ({progress:.2f}%) Saved: {title} ({pmc_id}) → Excel + MongoDB + Pinecone"
            )

        except Exception as e:
            print(f"❌ Error with {pmc_id}: {e}")

        time.sleep(2)
