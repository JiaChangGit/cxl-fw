# CXL Memory Tiering Lab

以 QEMU CXL Type-3 與 Linux MM 驗證記憶體上線、真實移頁、policy 對照、安全撤離及核心 trace。第一版 M0–M4／G0–G5 已完成，技術範圍與驗收以 [實作規格](cxl_memory_tiering_codex_implementation_v2.html) 為準。

## 教學

[從建置到核心追蹤](docs/cxl-mm-tutorial.md)可直接在 GitHub 閱讀，包含逐步建置、NUMA／CXL 原理、函式追蹤、關鍵程式碼、資料流與狀態圖、實際 BUG 定位及自問自答。預設具備 C/C++ 與 Linux 基本操作知識。另附[離線 HTML](docs/cxl-mm-tutorial.html)，下載後以瀏覽器開啟；圖表已內嵌，不需網路或 JavaScript。

下列 PASS 是 2026-11-06 在特定環境完成的歷史驗證結果。映像、run 原始紀錄與本機報告不包含在 Git 儲存庫中；新環境需依建置步驟重新執行，才能產生自己的證據。`make demo` 需要先有與目前輸入相符的完整 G0–G5 本機結果。

## 目前階段

- 開發規範與檢查：AGENTS.md、5 個按需載入的 PAEF skills，以及保留 foundation checks 的專案 quality gate。
- Host：Ubuntu 26.04／WSL、C/C++ 工具鏈、除錯與 QEMU 建置相依、KVM 權限。
- **M0／G0 PASS**：QEMU 11.1.0 與 Linux 6.18.56 自編、Ubuntu resolute rootfs、標準 systemd init；KVM 連續三次乾淨 boot/shutdown，三個不同 boot ID。
- **M1／G1-G2 PASS**：1 GiB volatile CXL → RAM region → DAX/kmem；8 個 128 MiB blocks 全部 online Movable。動態辨識 DRAM node 0 與 CXL node 1，沒有固定 slow nid。
- C++17 tierlab 對 64 MiB／16384 個 4 KiB 匿名頁執行 query/move/query；雙向各 16384/16384 成功，CXL 上重寫後返回 DRAM，完整性錯誤與缺值皆為 0。非法節點、非法位址及解除映射位址的真實負例皆通過。
- **M2／G3 PASS**：128 MiB／32768 頁、每輪 25000 ops，B0/B1/B2 各跑 seeds 1、7、42 與 60 epochs，共 540 輪。三 seeds 的 B2 最後 10 輪 ready HFR 中位數皆為 1.0，於 epoch 37（切換後第 8 輪）達到 ≥0.80；原訂門檻未調降。
- PolicyCore 只接 counts 與實際位置，含 EWMA／hysteresis、兩方向共用 attempts budget、實際 fast cap、cooldown 與 EBUSY backoff。Host 獨立重播 trace，以原始 kernel query 重算指標；完整性、query、cap 與 budget 全通過。
- B2 最後 10 輪 AWFR 約 89.96–90.10%；B1 約 2.29–2.33%，B0 為 0。這是 placement 結果，沒有宣稱實體 CXL speedup。
- **M3／G4 PASS**：128 MiB／32768 頁從 CXL 全移回 DRAM，query 與完整性錯誤皆為 0；解除 mapping 後，8 個白名單 blocks 全部 offline，再完成 devdax → disable-region → destroy-region，核對 region／DAX 已移除。
- Drain 讀取當下 DRAM node free／MemAvailable，保留 256 MiB 餘裕；每批最多 256 頁、每頁最多 3 次、Guest deadline 60 秒。Host 的 300 秒 VM timeout 處理 kernel syscall 卡住的情況，不並行重試。容量不足、partial rollback、rollback 再失敗與不可讀狀態由 FakeMigrator／MockSysfs 測試驗證，沒有冒稱真實 Guest 故障注入。
- **M4／G5 PASS**：隔離 trace instance／單一 reader、PID filter、monotonic markers、5 個 migration TAP cases、上游 MM migration baseline 與 Host 獨立 trace 對照。4 次 migration events 的成功數為 64、64、32、32；非法 target 的 trace 為空且 query 不變，全部 CPU 的 trace loss 為 0。
- 從實際 trace 衍生的缺事件、錯 batch 與截斷三個反例皆遭拒絕，明確標為 modeled；另有 29 個 validator 合成證據測試與 7 個 collector 測試，不冒稱真實 kernel 故障注入。
- **完整 make check PASS**：11 項 checks、G0–G5 真實 KVM Guest、4 項 C++ contracts 與 4 項 ASan／UBSan；最後補強後的 make check-quick 共 171 項 Python 測試 PASS。
- Host doctor 仍只檢查 Host 條件；實際 Guest 結果以 results/gates/G0.json、M1.json、M2.json、M3.json、M4.json 與各 run 原始紀錄為準。

## 使用方式

在 WSL 的專案根目錄執行（以下假設 checkout 位於 `~/src/cxl-fw`，請依實際位置調整）：

~~~bash
make doctor       # 工具、套件、資源與 KVM API/空 VM 檢查
make check-quick  # 規格/來源、設定與 Python 單元測試
make check        # Host、C++/sanitizer、三次 boot、M1–M4 Guest 驗證
make boot-test    # 單獨重跑 G0（每次使用新的 qcow2 overlay）
make migrate-test # 建 tierlab，單獨重跑 M1／G1-G2
make policy-test  # native core/FakeMigrator 測試，再跑 M2／G3
make offline-test # drain、逐 block offline 與 teardown，跑 M3／G4
make trace-test   # upstream migration baseline、syscall/trace/query，跑 M4／G5
make demo         # 重算已保存的 G0–G5 證據，不建置、不啟 VM
make tierlab-test # Host 假 syscall/core 契約測試，不操作 Host 頁面位置
~~~

從 Windows PowerShell 可以直接執行：

~~~powershell
wsl.exe -d Ubuntu-26.04
~~~

進入 WSL 後，執行 `cd ~/src/cxl-fw` 和 `make check`。

Host 條件報告存於 results/host/environment.json；完整檢查報告位於 .paef/reports/latest-full.json；Guest gates 位於 results/gates/。各 run 保存命令、input hashes、console、QMP、dmesg；M1 另存 CXL/DAX/sysfs、逐頁原始移頁紀錄與 Host 重算的 migration_verified.json。M2 的 shared/policy.jsonl 保存完整 counts、before/window_end/after、逐批 rc/errno/status、掃描與干擾計數器，policy_verified.json 保存 Host 重算的逐輪 HFR/AWFR、cold-start 與穩態指標。每個 M2 run 原始紀錄約 144 MiB，Host 串流驗證。M3 另存 shared/drain.jsonl、offline.jsonl 與 CLI 證據，offline 失敗時保存逐 block 快照，由 offline_verified.json 重算 G4。M4 保存 trace_batches.jsonl、trace.log、trace_formats/、collector.json、兩份 TAP 與 upstream baseline build provenance；trace_verified.json 逐批核對 markers、實際 migration events、query 與 loss counters。這些都是本機產物，不提交 Git。make doctor 不啟 Guest；make boot-test、make migrate-test、make policy-test、make offline-test、make trace-test、make check 會啟動指定 QEMU。

若要在另一台相同 Ubuntu 26.04 重建 Host：

~~~bash
make setup-host
make build BUILD_JOBS=2
make check
~~~

setup-host 僅安裝 [套件清單](configs/host-packages.txt) 中欠缺的項目，將目前使用者加入 kvm 群組。它不 upgrade 系統、不改 .wslconfig／裝置 mode，也不建置 QEMU 或 Guest。群組變更後需使用新登入程序；若原 session 還沒取得群組，先重新登入再檢查。

系統套件的版本以當時 Ubuntu repository 為準，doctor 保存實際版本；這不是可重建同一 Host image 的鎖檔。Host qemu-img 管理 overlay，Guest 一律由自編的釘版 QEMU 11.1.0 啟動。

## 從 PAEF 採用什麼

| 採納 | 用途 |
|---|---|
| quality_gate.py | 用明確 argv 執行必要檢查，失敗不能被 local override 覆蓋 |
| code-review、bug-rca | 變更審查與具體故障定位 |
| cpp-rust-safety | C/C++ lifetime、overflow、UB 與並行風險 |
| kernel-rca、qemu-gdb-tracing | 未來 Guest kernel 故障與 trace 調查 |

Skills 位於 .agents/skills，不複製全域 skills 或其他 Agent 的鏡像。原始檔未修改，來源、版本、ZIP SHA256 與檔案 hashes 見 [.paef/upstream.json](.paef/upstream.json)，授權見 [.paef/LICENSE](.paef/LICENSE)。

未安裝 PAEF manager、MCP server、hooks、CI 或通用 profiles。使用專案自有的 cxl-m4 profile，保留 Host checks，加入實際 CMake/CTest、ASan/UBSan 與 Guest gates。後續升級需先比對採納檔，不直接對此 checkout 執行 manager update。

## 完成範圍

第一版 M0–M4／G0–G5 已完成。最後完整驗證為 2026-11-06（台灣時間），報告見 .paef/reports/latest-full.json，最後 Python／規格檢查見 latest-quick.json，展示重算見 results/demo.json。R1 DAMON observer 與 R2 live offlining 是第一版之後的獨立實驗，未列入這次完成條件。M1–M4 的 Host evaluator 從 kernel query 與原始證據重算結果，不使用 policy 的狀態作評分。

五分鐘展示：執行 make demo，依序看拓樸、單批 syscall／trace／query、B0/B1/B2 phase shift 指標、invalid target 與 offline guard，再開輸出指向的原始證據。demo 會重算已保存的實際 Guest 結果，拒絕與目前 binary/image 不同的舊結果，不啟動 VM 或編譯。追查完整的 trace 重新執行 make trace-test。

## 建置與來源

~~~bash
make build BUILD_JOBS=2  # 依序 QEMU、kernel、tierlab、上游 MM selftest、rootfs；rootfs 階段需 sudo
~~~

版本與來源雜湊固定於 configs/versions.lock.json。QEMU 初始來源以官方 release manager 的 detached signature 驗證；Linux 以官方 HTTPS checksum 表核對。後續 scripts/build.py 下載與重跑都核對鎖定 SHA256，不自動升版。來源驗證資料存 downloads/ 與 build/qemu-source-signature.log。

所有重型 build／VM 使用 build/heavy.lock；不與其他 VM/build 同時執行。成功 rootfs 可重用；輸入改變或失敗產物需依 builder 訊息檢查，不會無條件遞迴刪除目錄。新機完整重建需先 make setup-host；rootfs 的套件版本與映像 hash 另外記於 images/rootfs.packages.tsv／rootfs.meta.json，不宣稱套件鏡像未來仍可重建相同 image。

本專案核心僅供 QEMU 測試：啟用上游 CONFIG_CXL_REGION_INVALIDATION_TEST，允許 Hypervisor 下的 region 配置；6.18.56 的 x86 路徑原本會拒絕此 cache invalidation。這不驗證實體硬體 cache 一致性，也不作為實體 CXL 部署設定。CONFIG_ZONE_DEVICE=y 另外提供 devdax 所需的 page map；只啟用 DEV_DAX 不足以完成模式切換。DRAM 以明確 NUMA backend 產生 SRAT；CXL 節點、位址與 block 範圍由 Guest 讀取。所有 CXL/sysfs 與真實 move_pages 操作僅發生在指定 Guest。

## 安全與公開範圍

`make setup-host` 與 rootfs 建置使用 sudo；執行前請檢視腳本與來源。Guest 的 9p 共用目錄可寫，並以執行 QEMU 的 Host 使用者權限存取；只提供可信任的 Guest 程式與本次 run 必需的檔案，不要放入私鑰、憑證或其他私人資料。Guest 身分檢查是避免誤操作的保護，並非用來隔離不可信程式的安全邊界。

`.gitignore` 排除本機映像、紀錄、建置產物與常見憑證檔案，但不能偵測所有敏感資料。分享紀錄或手動使用 `git add -f` 前，仍需自行檢查內容。採用的 PAEF 檔案保留 [.paef/LICENSE](.paef/LICENSE)，離線圖表使用的 Mermaid 11.12.1 授權見 [docs/MERMAID-LICENSE.txt](docs/MERMAID-LICENSE.txt)；這些授權不會自動套用到本專案的其他原創檔案。
