"""ChromaDB index over the document packs.

One chunk per PDF page, carrying metadata (shipment_id, doc_type, file,
page) so every retrieval result can be cited as "document, page". The
collection persists to .chroma/ so re-runs skip re-embedding.
"""
from __future__ import annotations

from pathlib import Path

import chromadb

from tools.embeddings import LocalHashEmbedding
from tools.pdf_extract import ExtractedDoc

COLLECTION = "freight_docs"


def get_client(persist_dir: str | None = ".chroma") -> chromadb.api.ClientAPI:
    if persist_dir:
        return chromadb.PersistentClient(path=persist_dir)
    return chromadb.Client()


def build_index(docs: list[ExtractedDoc], persist_dir: str | None = ".chroma"):
    client = get_client(persist_dir)
    try:
        client.delete_collection(COLLECTION)
    except Exception:
        pass
    col = client.create_collection(COLLECTION, embedding_function=LocalHashEmbedding(),
                                   metadata={"hnsw:space": "cosine"})
    ids, texts, metas = [], [], []
    for doc in docs:
        for page_no, text in enumerate(doc.pages, start=1):
            if not text.strip():
                continue
            ids.append(f"{doc.shipment_id}/{doc.doc_type}/p{page_no}")
            # prefix the chunk with its identity so lexical retrieval
            # lands on the right shipment even for short queries
            texts.append(f"[{doc.shipment_id} {doc.doc_type.replace('_', ' ')}] {text}")
            metas.append({
                "shipment_id": doc.shipment_id,
                "doc_type": doc.doc_type,
                "file": str(Path(doc.file)),
                "page": page_no,
            })
    col.add(ids=ids, documents=texts, metadatas=metas)
    return col


def load_index(persist_dir: str | None = ".chroma"):
    client = get_client(persist_dir)
    return client.get_collection(COLLECTION, embedding_function=LocalHashEmbedding())


def search(col, query: str, top_k: int = 5, shipment_id: str | None = None) -> list[dict]:
    where = {"shipment_id": shipment_id} if shipment_id else None
    res = col.query(query_texts=[query], n_results=top_k, where=where)
    hits = []
    for i in range(len(res["ids"][0])):
        dist = res["distances"][0][i]
        hits.append({
            "text": res["documents"][0][i],
            "meta": res["metadatas"][0][i],
            "distance": dist,
            "similarity": round(max(0.0, 1.0 - dist), 3),
        })
    return hits
