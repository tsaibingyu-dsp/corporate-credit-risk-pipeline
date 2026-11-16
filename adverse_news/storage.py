"""Atomic JSON snapshots with bounded Windows file-lock retries."""
import json
import os
import tempfile
import time
from pathlib import Path


def write_json(path,value):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    data=json.dumps(value,ensure_ascii=False,indent=2).encode('utf-8')
    # Avoid replacing unchanged files (e.g. an empty audit sample before round two).
    try:
        if path.read_bytes()==data:
            return
    except (FileNotFoundError,PermissionError):
        pass
    fd,name=tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=path.parent)
    temporary=Path(name)
    with os.fdopen(fd,'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    for attempt in range(6):
        try:
            temporary.replace(path)
            return
        except PermissionError as exc:
            if attempt==5:
                error=PermissionError(f'無法更新 {path}；已重試 6 次。新資料保留於 {temporary}。請關閉占用檔案的程式並確認資料夾寫入權限。')
                error.recovery_path=str(temporary.resolve())
                raise error from exc
            time.sleep(min(.15*2**attempt,2.4))
