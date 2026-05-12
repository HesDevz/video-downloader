# Douyin Downloader Local

A small local web app for downloading public Douyin videos from a copied share link.

## What it does

- Opens a local page at `http://localhost:8787`
- Accepts a Douyin share URL or full copied share text
- Downloads the public video to `/Users/zhuangjiujiu/Desktop/下载`

## Run

```bash
python3 -m douyin_downloader.server
```

Then open:

```text
http://localhost:8787/
```

## Test

```bash
python3 -m unittest douyin_downloader.tests.test_core
```

## Notes

This tool is for personal downloading of public videos. It does not bypass login, private content, paid content, or platform access controls.
