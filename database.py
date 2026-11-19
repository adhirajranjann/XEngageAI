"""MongoDB Atlas store of processed posts (duplicate prevention + history)."""

from __future__ import annotations

from datetime import datetime, timezone

from utils import log


class Store:
    """MongoDB Atlas store of processed posts (falls back to memory if no URI is set)."""

    def __init__(self, uri: str, db_name: str):
        self.readonly = False
        self._mem: dict[str, dict] = {}
        self._coll = None
        if uri:
            import certifi
            from pymongo import ASCENDING, MongoClient

            client = MongoClient(uri, serverSelectionTimeoutMS=8000, tlsCAFile=certifi.where())
            client.admin.command("ping")
            self._coll = client[db_name]["posts"]
            self._coll.create_index([("post_id", ASCENDING)], unique=True)
            log.info("Connected to MongoDB (db=%s)", db_name)
        else:
            log.warning("MONGODB_URI not set - duplicate tracking is in memory only")

    def seen(self, post_id: str) -> bool:
        if self._coll is not None:
            return self._coll.count_documents({"post_id": post_id}, limit=1) > 0
        return post_id in self._mem

    def save(self, doc: dict) -> None:
        if self.readonly:
            return
        if self._coll is not None:
            self._coll.update_one({"post_id": doc["post_id"]}, {"$set": doc}, upsert=True)
        else:
            self._mem[doc["post_id"]] = doc

    def replies_today(self) -> int:
        start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        start = start.astimezone(timezone.utc)
        if self._coll is not None:
            return self._coll.count_documents({"status": "replied", "processed_at": {"$gte": start}})
        return sum(
            1 for d in self._mem.values() if d.get("status") == "replied" and d["processed_at"] >= start
        )
