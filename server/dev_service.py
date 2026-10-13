"""Portable Vercel dev launcher using its assigned service port."""
import os
import uvicorn

if __name__ == "__main__":
    # Services CLI currently forwards its development host via a local-only
    # header. The API accepts that header only from a loopback connection.
    os.environ["BILI_LOCAL_VERCEL_PROXY"] = "1"
    uvicorn.run("vercel_entry:app", host="127.0.0.1", port=int(os.environ.get("PORT", "9000")))
