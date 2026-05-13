"""
Shared Playwright response capture utilities.
"""

import hashlib
import inspect
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


def _slugify_url(url: str, max_length: int = 120) -> str:
    parsed = urlparse(url)
    parts = [parsed.netloc, parsed.path.strip('/')]
    if parsed.query:
        parts.append(parsed.query)
    slug = '_'.join(part for part in parts if part)
    slug = re.sub(r'[^A-Za-z0-9._-]+', '_', slug).strip('._-')
    return (slug or 'response')[:max_length]


def _response_path(output_dir: Path, prefix: str, index: int, url: str, suffix: str) -> Path:
    url_hash = hashlib.sha1(url.encode('utf-8')).hexdigest()[:10]
    slug = _slugify_url(url)
    base = f"{prefix}_{index:03d}__{slug}__{url_hash}.{suffix}"
    path = output_dir / base
    duplicate = 1
    while path.exists():
        path = output_dir / f"{prefix}_{index:03d}__{slug}__{url_hash}_{duplicate}.{suffix}"
        duplicate += 1
    return path


async def _call_callback(callback, *args):
    if not callback:
        return None
    result = callback(*args)
    if inspect.isawaitable(result):
        return await result
    return result


def setup_response_capture(
    page,
    output_dir,
    captured: dict = None,
    json_should_save=None,
    on_document=None,
    on_json=None,
    log: bool = True,
) -> dict:
    """Attach a response listener that records and saves HTML/JSON responses.

    Saved filenames include a sequence number, a URL-derived slug, and a short
    URL hash, so unrelated document responses cannot overwrite each other.
    Publisher-specific extraction should be implemented in callbacks.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if captured is None:
        captured = {}
    captured.setdefault('json_responses', [])
    captured.setdefault('documents', [])
    captured.setdefault('document', None)
    captured.setdefault('timeline', [])

    async def handle_response(response):
        rtype = response.request.resource_type
        status = response.status
        url_str = response.url
        ts = datetime.now().isoformat()

        captured['timeline'].append({
            'timestamp': ts,
            'type': rtype,
            'status': status,
            'url': url_str,
            'method': response.request.method,
        })

        if log and status == 200:
            print(f"[{status}] {rtype:10s} {url_str[:70]}")

        if status != 200:
            return

        if rtype == 'document':
            try:
                html = await response.text()
                html_path = _response_path(
                    output_dir,
                    'page',
                    len(captured['documents']),
                    url_str,
                    'html',
                )
                html_path.write_text(html, encoding='utf-8')

                entry = {
                    'url': url_str,
                    'timestamp': ts,
                    'size': len(html),
                    'file': str(html_path),
                }
                captured['document'] = entry
                captured['documents'].append(entry)

                print(f"  ✓ HTML文档: {len(html)} 字节")
                print(f"    保存到: {str(html_path)}")
                await _call_callback(on_document, response, html, entry, captured)
            except Exception:
                pass
            return

        if rtype not in ('xhr', 'fetch'):
            return

        try:
            ctype = response.headers.get('content-type', '')
            if 'json' not in ctype.lower():
                return

            jdata = await response.json()
            jstr = json.dumps(jdata)
            if json_should_save and not json_should_save(response, jdata, jstr):
                return

            print(f"  ✓✓ API数据: {len(jstr)} 字节")

            jpath = _response_path(
                output_dir,
                'api_response',
                len(captured['json_responses']),
                url_str,
                'json',
            )
            with open(jpath, 'w', encoding='utf-8') as f:
                json.dump(jdata, f, indent=2, ensure_ascii=False)

            entry = {
                'url': url_str,
                'timestamp': ts,
                'size': len(jstr),
                'file': str(jpath),
            }
            captured['json_responses'].append(entry)
            await _call_callback(on_json, response, jdata, jstr, entry, captured)
        except Exception:
            pass

    page.on("response", handle_response)
    return captured
