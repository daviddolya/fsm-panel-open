# FSM Panel 3.1.2 static recovery checkpoint

This folder contains a curated report, metadata catalogue, and a verifier. It contains no original executable, decoded payload, credential values, account records, recovered script source, or private key material. The large private evidence remains outside this folder.

Target SHA-256: `3731a48590e3169071b81aab9338a36b38a788133adfd43fada99ad3add1f308`.

Run the verifier against a separately supplied, authorized copy of that exact artifact:

```powershell
python verify_panel_constants.py 'C:\path\to\Panel.exe' --test
```

Use CPython 3.11 to include the inert marshal check. Other Python versions skip that one test. The verifier reads the executable as bytes, validates its identity, decodes the constants into memory, walks every constant stream, and prints only integrity results. It neither executes target code nor writes decoded values. Run without `--test` to print module names, counts, sizes, and digests.

`verification-results.txt` records all six tests passing on local CPython 3.11.16. `feature-catalogue.json` contains all 458 module identities, 22 selected native/function mappings, config consumer names, and 36 curated UI/log strings. Addresses are preferred VAs at image base `0x140000000`; ASLR-adjust them for any future authorized runtime investigation.

Read `STATIC_FINDINGS.md` for evidence levels, launch/workflow findings, and unresolved behavior. Candidate function roles are not an implementation specification by themselves. No claim of runtime parity is made.
