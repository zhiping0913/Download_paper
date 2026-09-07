#!/usr/bin/env python3
"""Render paper.md for a ScienceDirect capture that was collected elsewhere.

Some articles are unreachable from the local network, so the HTML and the
sdfe/arp body JSON get fetched on a machine that does have access and are
dropped into a capture directory by hand. This script turns that offline
capture into the same paper.md the live pipeline would have produced —
without touching the network.

Expected layout (only html/ is required; the rest is used if present)::

    <paper_dir>/
        html/page.html        the article page as served
        html/body.json        https://www.sciencedirect.com/sdfe/arp/pii/<PII>/body?...
        metadata.json         optional; existing fields are preserved
        *_lrg.jpg             figure images, named as on ars.els-cdn.com
        paper.pdf             optional

Usage::

    python tools/render_captured_sciencedirect.py "<paper_dir>" [--doi DOI]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from publisher.sciencedirect import ScienceDirectHandler  # noqa: E402


def _local_figure_map(paper_dir: Path, figure_urls: dict) -> dict:
    """Map figure number -> local filename by matching URL basenames.

    The downloader names files after the CDN basename, so a capture collected
    by hand keeps the same names and the match is exact. Falls back to a
    ``grN``/``fgN`` stem match for files renamed along the way.
    """
    on_disk = {p.name: p.name for p in paper_dir.iterdir() if p.is_file()}
    stems = {}
    for name in on_disk:
        m = re.search(r'-((?:gr|fg|ga)\d+[a-z]?)_', name)
        if m:
            stems.setdefault(m.group(1), name)

    mapping = {}
    for fig_id, info in (figure_urls or {}).items():
        num = re.search(r'(\d+)$', str(fig_id))
        if not num:
            continue
        url = (info.get('url') if isinstance(info, dict) else str(info)) or ''
        basename = url.rsplit('/', 1)[-1]
        if basename in on_disk:
            mapping[num.group(1)] = basename
            continue
        m = re.search(r'-((?:gr|fg|ga)\d+[a-z]?)_', basename)
        if m and m.group(1) in stems:
            mapping[num.group(1)] = stems[m.group(1)]
    return mapping


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('paper_dir')
    ap.add_argument('--doi', default='')
    args = ap.parse_args()

    paper_dir = Path(args.paper_dir).expanduser().resolve()
    html_dir = paper_dir / 'html'
    page_html_path = html_dir / 'page.html'
    body_json_path = html_dir / 'body.json'

    if not page_html_path.is_file():
        print(f"✗ 缺少 {page_html_path}")
        return 1

    metadata_path = paper_dir / 'metadata.json'
    metadata = {}
    if metadata_path.is_file():
        metadata = json.loads(metadata_path.read_text(encoding='utf-8'))

    doi = args.doi or metadata.get('doi') or ''
    handler = ScienceDirectHandler(doi=doi, captured_data_dir=html_dir)

    page_html = page_html_path.read_text(encoding='utf-8', errors='replace')

    # Metadata from the page's own <meta> tags and __PRELOADED_STATE__ fills
    # anything the hand-written metadata.json left blank; existing values win.
    derived = handler._extract_metadata_from_html_meta(page_html) or {}
    state = handler._extract_preloaded_state(page_html) or {}
    authors = handler._authors_from_state(state) or []
    if authors and len(authors) > len(metadata.get('authors') or []):
        derived['authors'] = authors
    for key, value in derived.items():
        if value and not metadata.get(key):
            metadata[key] = value
    if doi:
        metadata['doi'] = doi

    # Body: the sdfe/arp JSON carries source MathML, so equations come out as
    # real LaTeX. Without it we fall back to the DOM walk inside
    # convert_to_markdown.
    figure_urls = {}
    if body_json_path.is_file():
        body_json = json.loads(body_json_path.read_text(encoding='utf-8'))
        rendered = handler.render_body_json(body_json)
        if rendered.get('body_md'):
            metadata['_body_md'] = rendered['body_md']
        if rendered.get('footnotes_md'):
            metadata['_footnotes_md'] = rendered['footnotes_md']
        figure_urls = rendered.get('figure_urls') or {}
        print(f"  ✓ 正文: {len(rendered.get('body_md') or ''):,} 字符, "
              f"{len(figure_urls)} 图")
    else:
        print("  ⚠️  没有 body.json，正文将从 page.html 提取（公式可能已被渲染）")

    # References live in the page DOM, not in the body JSON.
    if not metadata.get('references'):
        try:
            refs = handler.extract_references_from_html(page_html) or []
        except Exception as exc:
            print(f"  ⚠️  参考文献提取失败: {type(exc).__name__}: {exc}")
            refs = []
        if refs:
            metadata['references'] = refs
            print(f"  ✓ 参考文献: {len(refs)} 条")

    figure_filenames = _local_figure_map(paper_dir, figure_urls)
    print(f"  ✓ 本地图片: {len(figure_filenames)} 个")

    md = handler.convert_to_markdown(
        metadata,
        page_html,
        add_figure_refs=bool(figure_filenames),
        figure_filenames=figure_filenames,
        figure_urls=figure_urls,
        supplemental_urls=[],
        supplemental_descriptions={},
        supplemental_downloads=[],
    )

    md_path = paper_dir / 'paper.md'
    md_path.write_text(md, encoding='utf-8')
    print(f"  ✓ {md_path} ({len(md.splitlines())} 行)")

    # Refresh metadata.json with what the render actually used, minus the
    # private render-only keys.
    public = {k: v for k, v in metadata.items() if not k.startswith('_')}
    public['extracted_at'] = datetime.now().isoformat()
    if (paper_dir / 'paper.pdf').is_file():
        public['pdf'] = 'paper.pdf'
    metadata_path.write_text(
        json.dumps(public, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"  ✓ {metadata_path}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
