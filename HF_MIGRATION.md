# Chuyển dataset từ Git → Hugging Face

Mục tiêu: ngừng commit dataset vào git (repo `.git` đã phình **6.9GB**), chuyển sang
lưu trên Hugging Face Dataset `ledhuy/crawl_news_vn` (private). Crawl daily không
còn bị giới hạn lưu trữ của GitHub.

Có **3 việc**: (1) cấu hình HF token, (2) migrate data hiện có lên HF, (3) dọn lịch
sử git để thu hồi 6.9GB.

---

## 1. Tạo HF token + set GitHub secret

1. Vào <https://huggingface.co/settings/tokens> → **New token** → quyền **Write**.
2. Trong repo GitHub: **Settings → Secrets and variables → Actions → New repository
   secret**:
   - Name: `CRAWL_NEWS_VN`
   - Value: token vừa tạo.

Workflow `.github/workflows/crawl.yml` map `secrets.CRAWL_NEWS_VN` → env `HF_TOKEN` sẵn.
Repo-id để mặc định `ledhuy/crawl_news_vn` (đổi qua env `HF_REPO_ID` nếu muốn).

> Lưu ý private: free tier HF cho **public** dataset là 1TB, còn **private** quota nhỏ
> hơn nhiều. Dataset của bạn vài trăm MB nên thoải mái, nhưng nếu sau này phình to
> cân nhắc để public (đề tài học thuật, share community) để được full 1TB.

---

## 2. Migrate data hiện có lên HF (chạy 1 lần, ở máy local)

```bash
pip install "huggingface_hub>=0.24"

export HF_TOKEN=hf_xxx                       # token Write
export HF_REPO_ID=ledhuy/crawl_news_vn
export HF_PRIVATE=1

# Đẩy state nóng (articles_ner* + checkpoint_*) — đủ cho pipeline chạy tiếp:
python hf_sync.py push

# HOẶC đẩy backup đầy đủ cả file rebuildable (rss/html/final/cleaned):
python hf_sync.py push --all
```

PowerShell:

```powershell
$env:HF_TOKEN="hf_xxx"; $env:HF_REPO_ID="ledhuy/crawl_news_vn"; $env:HF_PRIVATE="1"
python hf_sync.py push
```

Kiểm tra: mở <https://huggingface.co/datasets/ledhuy/crawl_news_vn> → tab **Files**.

Test pull ngược (vào thư mục trống hoặc máy khác):

```bash
python hf_sync.py pull        # tải articles_ner* + checkpoint_* về data/
```

---

## 3. Dọn 6.9GB `.git` history

> ⚠️ **Rewrite history + force-push.** Báo cho mọi collaborator (nếu có) re-clone sau
> khi xong. **Backup repo trước**: copy cả folder, hoặc `git clone --mirror` ra chỗ khác.

Cách an toàn nhất là `git filter-repo` (nhanh & chuẩn hơn `filter-branch`).

```bash
pip install git-filter-repo        # hoặc: brew install git-filter-repo

cd crawl_news_v2

# Backup phòng hờ:
git clone --mirror . ../crawl_news_v2_backup.git

# Xoá toàn bộ thư mục data/ khỏi MỌI commit trong history:
git filter-repo --path data/ --invert-paths --force

# filter-repo gỡ remote -> add lại:
git remote add origin https://github.com/<user>/crawl_news_v2.git

# Dọn rác + nén lại:
git reflog expire --expire=now --all
git gc --prune=now --aggressive

# Kiểm tra size mới:
du -sh .git        # kỳ vọng từ 6.9GB -> vài MB

# Ghi đè remote (history mới):
git push origin --force --all
git push origin --force --tags
```

Sau bước này repo GitHub chỉ còn code. Data sống trên HF, đồng bộ tự động qua workflow.

### Nếu không muốn rewrite history

Có thể tạo repo GitHub mới sạch, `git init` lại, commit code hiện tại (đã có
`.gitignore` chặn data) rồi push. Đơn giản, không động history cũ, nhưng mất số liệu
commit/PR cũ.

---

## 4. Workflow daily đã đổi như thế nào

`.github/workflows/crawl.yml` giờ chạy: **pull HF → run_all.py → push HF** (thay vì
commit git). Nhờ pull state cũ trước khi chạy nên:

- NER vẫn **incremental** (skip ID đã NER, không re-NER 4h mỗi ngày).
- Crawler vẫn resume + dedup qua `checkpoint_*.json`.

`permissions` hạ xuống `contents: read` (không cần quyền ghi repo nữa).

---

## TL;DR

| Việc | Lệnh |
|---|---|
| Set secret | GitHub Settings → Actions secret `HF_TOKEN` |
| Migrate lần đầu | `python hf_sync.py push` (hoặc `--all`) |
| Test | `python hf_sync.py pull` |
| Dọn git | `git filter-repo --path data/ --invert-paths --force` → `git gc` → force-push |
