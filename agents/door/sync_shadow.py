"""python3 -m agents.door.sync_shadow --shadow — sync 미리보기의 구성 뿌리(쓰기 0)."""

from __future__ import annotations

import sys

from ohmyboring.sync.shadow import main

from ..shared import vault_note

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], vault_note.split_frontmatter))
