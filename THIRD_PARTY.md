# 外部ソフトウェア

このプロジェクトは [Dejniel/TiMini-Print](https://github.com/Dejniel/TiMini-Print) の commit `4aaf9459f1d7854bdca5bafffd86756704b76376` を `vendor/TiMini-Print` に同梱しています。上流ソースを変更せず、配布版を識別する `PINNED_COMMIT.txt` のみ追加しています。著作権と帰属は上流の [NOTICE](vendor/TiMini-Print/NOTICE)、利用条件は [LICENSE](vendor/TiMini-Print/LICENSE)、上流が示す第三者ライセンスは [THIRD_PARTY_NOTICES.md](vendor/TiMini-Print/THIRD_PARTY_NOTICES.md) と `licenses` を参照してください。

Pillow、OpenCV、qrcode、および印刷用ライブラリは、`pyproject.toml` と `uv.lock` に従って `uv` でそれぞれの配布元から取得します。各ライブラリの利用条件はその配布物のライセンスを参照してください。本配布物には日本語フォントを同梱していません。既定の Meiryo は利用する Windows 環境に存在するファイルを読み込みます。
