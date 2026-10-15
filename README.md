# media-rankings

nas-app のメディアタブにある動画一覧3画面（**Tokyomotion / 85po / Gofile**）を、他のアプリへ組み込むために切り出したもの。

- 入口は **[AGENTS.md](AGENTS.md)**（組み込む AI にはこれだけ読ませる）。何が入っているか、ホスト側が用意するもの、検証方法が書いてある。
- 各画面の仕様は `docs/`。
- 元のコードは nas-app（`ae478b4`）からパス構成のまま原文コピー。接合点を抜粋した `server/schema/`・`media_fetch.py`・
  `web/src/routes/media/{api,types}.ts`・`web/src/styles/site-list.css` だけが加工品。

```bash
git clone https://github.com/temmie0232/media-rankings.git
cd media-rankings/server && uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m pytest tests/test_tokyomotion.py tests/test_po85.py   # 229 passed
```
