# Kiểm chứng tích hợp quán TV3 — Phase 1

- Nguồn: `quan.py`, `test_quan_rules.py` và `PRD-0.md` do TV3 cung cấp; đối chiếu `chonoi/sim/quan.py` hiện có và contract trong repo.
- Đã tích hợp luật khách, giữ nguyên liệu, tương tác bếp/bàn, boa, thanh toán, kết ca và test. Bổ sung kiểm fixture bàn/bếp và kiểu id món để đầu vào sai trả `BadInput`.
- Lệnh trên Windows: `$env:PYTHONPATH='tests:.'; python -m unittest discover -s tests -v` → **47 test, OK**.
- `python -m chonoi.headless --mode quan --seed 1 --ticks 121 --out <temp> --snapshot-every 120` → event `no_dish` tại tick 120, dòng `end` có `ticks:121`, `reason:tick_limit`.
- `git diff --check` → không có lỗi whitespace.
- Chưa sửa `game.contract.json`, `docs/contracts/` hoặc test có sẵn. Chưa commit/push; người gộp cần review thay đổi trước khi đẩy.
