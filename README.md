# Download_paper - Multi-Publisher Academic Paper Extraction System

[![Python](https://img.shields.io/badge/Python-3.9+-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green)]()

## 🎯 Overview

A modular, extensible system for automatically extracting and converting academic papers to Markdown format. Currently supports American Physical Society (APS) journals with architecture designed for easy addition of other publishers.

**Key Features:**
- 📄 Full metadata extraction (authors, affiliations, abstract, references)
- 🖼️ Figure extraction and embedding in Markdown
- 📐 LaTeX equation preservation and formatting
- 🔗 PDF download and supplemental materials handling
- 🔍 Network request monitoring for capturing fulltext API data
- 📚 Semantic Scholar API integration for metadata enrichment

## 🏗️ Architecture

```
Download_paper/
├── core/                          # Generic, reusable utilities
│   ├── __init__.py
│   └── utilities.py               # Semantic Scholar API, HTML parsing
├── publisher/                     # Publisher-specific implementations
│   ├── base.py                    # Abstract PublisherHandler class
│   ├── aps.py                     # APS implementation
│   └── aps.md                     # APS API documentation
├── complete_paper_extraction.py   # Main orchestration
├── json_to_md_converter.py        # JSON → Markdown converter
└── docs/                          # Documentation
```

## 🚀 Quick Start

```bash
# Activate environment
source /home/zhiping/research-env/bin/activate

# Extract a paper by DOI
python3 complete_paper_extraction.py "10.1103/PhysRevLett.109.245005"

# Output: ~/Downloads/papers/2012--Isolated attosecond pulses.../
#   ├── *.md (Markdown with figures)
#   ├── *.pdf (PDF)
#   ├── *.json (Metadata)
#   └── figure_*.png
```

## 📖 Documentation

For detailed information, see:
- **[docs/QUICKSTART.md](docs/QUICKSTART.md)** - Quick reference
- **[docs/REFACTORING.md](docs/REFACTORING.md)** - Architecture & progress
- **[docs/WORKFLOW_SUMMARY.md](docs/WORKFLOW_SUMMARY.md)** - Complete workflow
- **[docs/PROJECT_STRUCTURE.md](docs/PROJECT_STRUCTURE.md)** - Project details

## 🔧 Supported Publishers

- ✅ **APS** (American Physical Society) - Physical Review Letters, etc.
- ⏳ Nature journals (in progress)
- ⏳ Elsevier journals (planned)

## 📊 Phase Status

| Phase | Status | Details |
|-------|--------|---------|
| 1 | ✅ Done | Core modules & APS implementation |
| 2 | ✅ Done | Modular refactoring |
| 3 | ⏳ Next | Full publisher abstraction |
| 4 | 📋 Planned | Multi-publisher support |

## 🤝 Contributing

See [docs/](docs/) for development guidelines.

---

**Version**: 2.0 | **Status**: Production Ready | **Last Updated**: 2026-05-12