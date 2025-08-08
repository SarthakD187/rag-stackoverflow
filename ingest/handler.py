def lambda_handler(event, _ctx):
    return {"statusCode": 200, "body": "ingest stub"}

"""
ingest/handler.py
A tiny smoke-test: build an in-memory FAISS vector store with one
document and immediately query it back.
"""

import json
from langchain_community.embeddings import FakeEmbeddings
from langchain_community.vectorstores import FAISS

DOC_TEXT = "Hello world: first RAG document"                # placeholder corpus

def lambda_handler(event=None, _context=None):
    # 1️⃣  Index the single document
    vs = FAISS.from_texts([DOC_TEXT], FakeEmbeddings(size=1536))

    # 2️⃣  Retrieve something to prove it works
    result = vs.similarity_search("hello")[0].page_content

    return {
        "statusCode": 200,
        "body": json.dumps({"retrieved": result})
    }

# Allow `python ingest/handler.py` to run as a script
if __name__ == "__main__":
    print(lambda_handler())
