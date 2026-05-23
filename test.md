markdown_content = """# Tài liệu Kiến trúc Pipeline Xử lý Dữ liệu (Articles Data Pipeline)

Tài liệu này mô tả chi tiết luồng xử lý dữ liệu báo chí từ giai đoạn thu thập (Crawl) cho đến khi làm sạch và nạp vào các cơ sở dữ liệu lưu trữ cuối (Elasticsearch, ClickHouse, PostgreSQL).

---

## 1. Tổng quan Sơ đồ Luồng Dữ liệu (Data Flow)

Pipeline bao gồm **5 giai đoạn chính**. Mỗi giai đoạn vừa thực hiện biến đổi thuộc tính (field transformation), vừa áp dụng các bộ lọc để loại bỏ hoặc đánh dấu các bản ghi không hợp lệ.
