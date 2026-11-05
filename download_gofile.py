#!/usr/bin/env python3
"""
Worker that consumes GoFile URIs from RabbitMQ, downloads the files,
and publishes them to the pipeline's entry queue.
"""

import hashlib
import html
import json
import os
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

import config
from utils import (
    DownloadManager,
    LinkManager,
    RabbitMQConsumer,
    calculate_file_hash,
    get_db_engine,
    get_publisher,
    logger,
    setup_logging,
)

MAX_RETRIES = 3
RETRY_BACKOFF = 30

# --- GoFile Logic (extracted from external downloader) ---


@dataclass(frozen=True)
class RemoteFile:
    provider: str
    file_id: str
    name: str
    url: str
    size: int | None = None
    cookie_header: str | None = None

    @property
    def file_key(self) -> str:
        stable_id = self.file_id or hashlib.sha256(self.url.encode("utf-8")).hexdigest()
        return f"external:{self.provider}:{stable_id}"


def build_headers(
    token: str | None = None,
    range_start: int | None = None,
    cookie_header: str | None = None,
) -> dict[str, str]:
    headers = {"User-Agent": "DataLeakSuperManager GoFileWorker/1.0", "Accept": "*/*"}
    cookie_parts = []
    if token:
        headers["Authorization"] = f"Bearer {token}"
        cookie_parts.append(f"accountToken={token}")
    if cookie_header:
        cookie_parts.append(cookie_header)
    if cookie_parts:
        headers["Cookie"] = "; ".join(cookie_parts)
    if range_start and range_start > 0:
        headers["Range"] = f"bytes={range_start}-"
    return headers


def http_json(url: str, token: str | None = None) -> dict:
    request = Request(url, headers=build_headers(token))
    try:
        with urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as e:
        raise RuntimeError(
            f"HTTP Error {e.code}: {e.reason}"
        ) from e


def http_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"})
    try:
        with urlopen(request, timeout=60) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            text = response.read().decode(charset, errors="replace")
            if "This content does not exist" in text:
                raise RuntimeError("FILE_NOT_FOUND")
            return text
    except HTTPError as e:
        raise RuntimeError(
            f"HTTP Error {e.code}: {e.reason}"
        ) from e


def sanitize_filename(name: str) -> str:
    clean = unquote(name or "").strip().replace("\\", "_").replace("/", "_")
    clean = re.sub(r"[\x00-\x1f\x7f]+", "_", clean)
    clean = re.sub(r"\s+", " ", clean)
    return clean or "downloaded_file"


def unique_destination_name(output_dir: Path, filename: str) -> str:
    filename = sanitize_filename(filename)
    stem, suffix = Path(filename).stem, Path(filename).suffix
    counter = 1
    candidate = filename
    while (output_dir / candidate).exists() or (
        output_dir / f"{candidate}.part"
    ).exists():
        candidate = f"{stem}_{counter}{suffix}"
        counter += 1
    return candidate


def extract_gofile_content_id(uri: str) -> str | None:
    parsed = urlparse(uri)
    if parsed.netloc.lower().endswith("gofile.io"):
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "d":
            return parts[1]
        if parts:
            return parts[-1]
    return None


def iter_gofile_files(
    content_id: str, token: str | None, password: str | None, max_depth: int
) -> Iterable[RemoteFile]:
    password_hash = (
        hashlib.sha256(password.encode("utf-8")).hexdigest() if password else None
    )
    yield from _iter_gofile_content(content_id, token, password_hash, 0, max_depth)


def _iter_gofile_content(
    content_id: str,
    token: str | None,
    password_hash: str | None,
    depth: int,
    max_depth: int,
) -> Iterable[RemoteFile]:
    if depth > max_depth:
        return
    query = f"?password={password_hash}" if password_hash else ""
    payload = http_json(f"https://api.gofile.io/contents/{content_id}{query}", token)
    if payload.get("status") != "ok":
        raise RuntimeError(f"GoFile API error: {payload}")
    yield from _walk_gofile_node(
        payload.get("data") or {}, token, password_hash, depth, max_depth, False
    )


def _walk_gofile_node(
    node: dict,
    token: str | None,
    password_hash: str | None,
    depth: int,
    max_depth: int,
    fetch_empty: bool,
    cookie_header: str | None = None,
) -> Iterable[RemoteFile]:
    node_type = (node.get("type") or "").lower()
    node_id = str(node.get("id") or node.get("code") or "")
    name = sanitize_filename(str(node.get("name") or node_id or "downloaded_file"))
    link = node.get("link") or node.get("directLink") or node.get("downloadLink")
    if node_type == "file" or link:
        if not link:
            return
        yield RemoteFile(
            "gofile", node_id, name, str(link), node.get("size"), cookie_header
        )
        return
    children = node.get("children") or {}
    child_nodes = (
        children.values()
        if isinstance(children, dict)
        else (children if isinstance(children, list) else [])
    )
    yielded = False
    for child in child_nodes:
        if isinstance(child, dict):
            yielded = True
            yield from _walk_gofile_node(
                child, token, password_hash, depth + 1, max_depth, True, cookie_header
            )
    if (
        fetch_empty
        and not yielded
        and node_id
        and node_type == "folder"
        and depth < max_depth
    ):
        yield from _iter_gofile_content(
            node_id, token, password_hash, depth + 1, max_depth
        )


def iter_gofile_public_page_files(uri: str) -> Iterable[RemoteFile]:
    page = http_text(uri)
    page = html.unescape(page).replace("\\/", "/")
    candidates = set()
    for match in re.finditer(r"https?://[^\"'<>\s]+", page):
        url = match.group(0).rstrip("),.;")
        if "gofile" in urlparse(url).netloc.lower() and (
            "/download" in url.lower()
            or re.search(r"\.(?:7z|rar|zip|txt)", url.lower())
        ):
            candidates.add(url)
    for url in sorted(candidates):
        yield RemoteFile(
            "gofile-public",
            hashlib.sha256(url.encode()).hexdigest(),
            Path(unquote(urlparse(url).path)).name,
            url,
        )


def iter_gofile_browser_files(
    uri: str, max_depth: int, timeout_ms: int, headless: bool
) -> list[RemoteFile]:
    try:
        from playwright.sync_api import TimeoutError as PTError
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("Playwright not installed")

    files = {}
    with sync_playwright() as p:
        # Use a more realistic user agent to avoid bot detection
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            accept_downloads=False,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        page = context.new_page()

        def on_res(res):
            if "api.gofile.io/contents/" in res.url:
                logger.debug("Intercepted GoFile API call: %s", res.url)
                try:
                    payload = res.json()
                    if payload.get("status") == "ok":
                        cookie = "; ".join(
                            f"{c['name']}={c['value']}" for c in context.cookies()
                        )
                        for r in _walk_gofile_node(
                            payload.get("data") or {},
                            None,
                            None,
                            0,
                            max_depth,
                            False,
                            cookie,
                        ):
                            files[r.url] = r
                except Exception as e:
                    logger.debug("Failed to parse GoFile API response: %s", e)

        page.on("response", on_res)
        try:
            # Use 'networkidle' to ensure the JS has finished fetching the
            # content
            page.goto(uri, wait_until="networkidle", timeout=timeout_ms)
            # Additional small sleep just in case
            page.wait_for_timeout(2000)
        except PTError:
            logger.warning("Browser timeout while loading %s", uri)
        browser.close()
    return list(files.values())


def resolve_remote_files(
    uri: str,
    token: str | None,
    password: str | None,
    max_depth: int,
    allow_api: bool,
    browser_resolve: bool,
    browser_timeout_ms: int,
    browser_headed: bool,
) -> list[RemoteFile]:
    cid = extract_gofile_content_id(uri)
    if cid:
        # 1. Try GoFile API
        if token or allow_api:
            try:
                res = list(iter_gofile_files(cid, token, password, max_depth))
                if res:
                    return res
                logger.warning("GoFile API returned no files for cid=%s", cid)
            except Exception as e:
                logger.debug("GoFile API resolution failed for %s: %s", uri, e)

        # 2. Try Public Page Scraping
        try:
            pub = list(iter_gofile_public_page_files(uri))
            if pub:
                return pub
        except Exception as e:
            logger.debug("Public page resolution failed for %s: %s", uri, e)

        # 3. Try Browser (Playwright)
        if browser_resolve:
            try:
                res = iter_gofile_browser_files(
                    uri, max_depth, browser_timeout_ms, not browser_headed
                )
                if res:
                    return res
                logger.warning("Browser resolution returned no files for uri=%s", uri)
            except Exception as e:
                logger.error("Browser resolution failed for %s: %s", uri, e)

        logger.error("All resolution methods failed for GoFile content %s", uri)
        return []

    # Fallback only if it's NOT a GoFile folder URI
    if not extract_gofile_content_id(uri):
        return [
            RemoteFile(
                "direct",
                hashlib.sha256(uri.encode()).hexdigest(),
                Path(unquote(urlparse(uri).path)).name,
                uri,
            )
        ]

    return []


# Removed duplicate extract_gofile_content_id definition


def download_remote_file(
    remote: RemoteFile, output_dir: Path, token: str | None, force: bool = False
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    saved = (
        unique_destination_name(output_dir, remote.name)
        if force
        else sanitize_filename(remote.name)
    )
    dest, part = output_dir / saved, output_dir / f"{saved}.part"
    if (
        dest.exists()
        and not force
        and (remote.size is None or dest.stat().st_size == remote.size)
    ):
        return dest
    offset = part.stat().st_size if part.exists() else 0
    req = Request(
        remote.url, headers=build_headers(token, offset, remote.cookie_header)
    )
    try:
        with urlopen(req, timeout=120) as res:
            if offset and getattr(res, "status", 200) != 206:
                offset = 0
                part.unlink(missing_ok=True)
                return download_remote_file(remote, output_dir, token, force=True)
            with open(part, "ab" if offset else "wb") as h:
                while True:
                    chunk = res.read(1024 * 1024)
                    if not chunk:
                        break
                    h.write(chunk)
    except HTTPError as e:
        raise RuntimeError(
            f"Download failed: HTTP Error {e.code}: {e.reason}"
        ) from e
    except Exception as e:
        raise RuntimeError(f"Download failed: {e}")
    os.replace(part, dest)
    return dest


# --- Worker Implementation ---


def _is_rate_limited(e: Exception) -> bool:
    msg = str(e)
    return "429" in msg or "Too Many Requests" in msg


def process_gofile_uri(ch, method, properties, body):
    try:
        msg = json.loads(body) if isinstance(body, bytes) else json.loads(body.decode())
        if not isinstance(msg, dict):
            msg = {"uri": msg.strip()}
        uri = msg.get("uri", "")
        retry_count = msg.get("retry_count", 0)

        if not uri:
            ch.basic_ack(delivery_tag=method.delivery_tag)
            return

        logger.info("Processing GoFile URI: %s (retry=%d/%d)", uri, retry_count, MAX_RETRIES)
        engine = get_db_engine()
        download_manager = DownloadManager(engine)
        link_manager = LinkManager(engine)
        publisher = get_publisher(config.RABBIT_CONNECTION_STRING, config.UNIQUES_QUEUE)
        gofile_publisher = get_publisher(config.RABBIT_CONNECTION_STRING, config.URI_GOFILE_QUEUE)

        def _requeue(error_desc: str):
            if retry_count < MAX_RETRIES:
                delay = (retry_count + 1) * RETRY_BACKOFF
                logger.warning(
                    "Rate limited for %s (attempt %d/%d). Re-queuing in %ds.",
                    uri, retry_count + 1, MAX_RETRIES, delay,
                )
                time.sleep(delay)
                gofile_publisher.publish({**msg, "retry_count": retry_count + 1})
                ch.basic_ack(delivery_tag=method.delivery_tag)
            else:
                link_manager.update_status(uri, "FAILED", error_desc)
                logger.warning(
                    "Marked URI %s as FAILED after %d retries: %s",
                    uri, MAX_RETRIES, error_desc,
                )
                ch.basic_ack(delivery_tag=method.delivery_tag)

        # Resolution
        try:
            remote_files = resolve_remote_files(
                uri,
                token=os.getenv("GOFILE_TOKEN"),
                password=None,
                max_depth=5,
                allow_api=True,
                browser_resolve=True,
                browser_timeout_ms=30000,
                browser_headed=False,
            )

            if not remote_files:
                logger.warning("No files resolved for URI: %s", uri)
                link_manager.update_status(
                    uri, "FAILED", "NOTFOUND: No files found in GoFile content"
                )
                ch.basic_ack(delivery_tag=method.delivery_tag)
                return

        except Exception as e:
            logger.error("Resolution failed for %s: %s", uri, e)
            if _is_rate_limited(e):
                _requeue(f"RATE_LIMITED: Resolution failed: {e!s}")
            else:
                link_manager.update_status(uri, "FAILED", f"ERROR: {e!s}")
                ch.basic_ack(delivery_tag=method.delivery_tag)
            return

        files_processed_successfully = 0
        last_error = None
        for remote in remote_files:
            try:
                if "cookies" in remote.name.lower():
                    logger.info(
                        "Skipping GoFile file %s - contains 'cookies'", remote.name
                    )
                    continue

                path = download_remote_file(
                    remote, Path(config.DOWNLOAD_DIR), os.getenv("GOFILE_TOKEN")
                )

                # Register in DB and publish
                file_hash = calculate_file_hash(str(path))
                saved_name = os.path.relpath(path, config.DOWNLOAD_DIR)

                file_id = download_manager.register_external_download(
                    remote.file_key, remote.name, saved_name, uri, file_hash
                )

                payload = {
                    "id": file_id,
                    "file_key": remote.file_key,
                    "saved_file_name": saved_name,
                }
                if publisher.publish(payload):
                    logger.info(
                        "Published GoFile file id=%s to %s",
                        file_id,
                        config.UNIQUES_QUEUE,
                    )
                    files_processed_successfully += 1

            except Exception as e:
                logger.error(
                    "Failed to process file %s from %s: %s", remote.name, uri, e
                )
                if _is_rate_limited(e):
                    last_error = e

        # If every file failed due to rate limiting, re-queue the URI
        if files_processed_successfully == 0 and last_error is not None:
            _requeue(f"RATE_LIMITED: All files failed: {last_error!s}")
            return

        # Update original URI status in discovered_links
        if files_processed_successfully > 0:
            link_manager.update_status(uri, "PROCESSED")
            logger.info(
                "Marked URI %s as PROCESSED (%d files)",
                uri,
                files_processed_successfully,
            )
        else:
            link_manager.update_status(
                uri, "FAILED", "ERROR: All files in folder failed to download"
            )
            logger.warning(
                "Marked URI %s as FAILED (0 files successfully processed)", uri
            )

        ch.basic_ack(delivery_tag=method.delivery_tag)
    except Exception as e:
        logger.error("Error in GoFile worker: %s", e)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)


def main():
    setup_logging()
    consumer = RabbitMQConsumer(
        url=config.RABBIT_CONNECTION_STRING,
        queue=config.URI_GOFILE_QUEUE,
        callback=process_gofile_uri,
    )
    logger.info(
        "GoFile Download Worker started. Listening on %s", config.URI_GOFILE_QUEUE
    )
    consumer.start()


if __name__ == "__main__":
    main()
