"""Stage C self-test — 不依赖 pytest，纯 Python 验收"""
import sys, os, io, copy
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src-python'))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

passes = 0
fails = 0

def check(name, condition, detail=""):
    global passes, fails
    if condition:
        passes += 1
        print(f"  [PASS] {name}")
    else:
        fails += 1
        print(f"  [FAIL] {name} — {detail}")

# ===== C.1: spec_schema.py =====
print("=== C.1: spec_schema.py ===")
from prompts.spec_schema import (
    validate_spec_structure, validate_spec_strict,
    build_spec_index, SPEC_JSON_SCHEMA, SPEC_MINIMAL_TEMPLATE,
    SPEC_FIELD_DESCRIPTIONS,
)

props = SPEC_JSON_SCHEMA["properties"]["spec"]["properties"]
check("SPEC_JSON_SCHEMA has 12 properties", len(props) == 12,
      f"got {len(props)}: {list(props.keys())[:5]}...")
check("SPEC_MINIMAL_TEMPLATE non-empty + valid YAML", len(SPEC_MINIMAL_TEMPLATE) > 100)
# Extra: verify template parses as valid YAML
import yaml as _yaml
try:
    _parsed = _yaml.safe_load(SPEC_MINIMAL_TEMPLATE)
    check("SPEC_MINIMAL_TEMPLATE is valid YAML", isinstance(_parsed, dict) and "spec" in _parsed)
    check("SPEC_MINIMAL_TEMPLATE has functional_requirements",
          "functional_requirements" in _parsed.get("spec", {}))
except Exception:
    check("SPEC_MINIMAL_TEMPLATE is valid YAML", False, "yaml.safe_load failed")

check("SPEC_FIELD_DESCRIPTIONS has 7+ fields", len(SPEC_FIELD_DESCRIPTIONS) >= 7,
      f"got {len(SPEC_FIELD_DESCRIPTIONS)}")
# Extra: verify expected keys exist
_expected_fd_keys = ["functional_requirements", "openapi", "schemas", "constraints",
                      "verification", "files_to_create", "files_to_modify"]
_missing_fd = [k for k in _expected_fd_keys if k not in SPEC_FIELD_DESCRIPTIONS]
check(f"SPEC_FIELD_DESCRIPTIONS has all 7 expected keys",
      len(_missing_fd) == 0, f"missing: {_missing_fd}")

# Valid spec
valid = {
    "spec": {
        "name": "Test Feature", "version": 1,
        "functional_requirements": [
            {"id": "FR-1", "description": "Do X"},
            {"id": "FR-2", "description": "Do Y", "acceptance": "check Z"},
        ],
        "constraints": ["password must be bcrypt hashed"],
        "files_to_create": ["src/models/x.py"],
    }
}
errs = validate_spec_structure(valid)
check("Valid spec: 0 errors", len(errs) == 0, f"got {errs}")

# Empty spec
errs = validate_spec_structure({"spec": {}})
check("Empty spec: >=3 errors", len(errs) >= 3, f"got {len(errs)}: {errs[:2]}")

# Missing FR description
errs = validate_spec_structure({"spec": {"name": "X", "version": 1, "functional_requirements": [{"id": "X-1"}]}})
check("Missing FR desc: >=1 error", len(errs) >= 1, f"got {len(errs)}")

# Strict mode with full valid spec
valid_full = copy.deepcopy(valid)
valid_full["spec"]["openapi"] = {"openapi": "3.0.3", "info": {"title": "X", "version": "1.0"}, "paths": {"/api": {"get": {}}}}
valid_full["spec"]["schemas"] = {"User": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}}
valid_full["spec"]["verification"] = {"commands": ["pytest"]}
errs = validate_spec_strict(valid_full)
check("Strict valid (with security): 0 errors", len(errs) == 0, f"got {errs}")

# Strict mode without security — use a spec with NO security keywords in constraints
no_sec = {
    "spec": {
        "name": "Test", "version": 1,
        "functional_requirements": [{"id": "FR-1", "description": "Do X"}],
        "constraints": ["code must follow PEP8", "functions < 50 lines"],
        "files_to_create": ["src/x.py"],
    }
}
errs = validate_spec_strict(no_sec)
check("Strict (no security): warns about security", any("安全" in e for e in errs),
      f"errors: {errs}")

# build_spec_index
index = build_spec_index(valid_full)
check("Index: name correct", index["name"] == "Test Feature")
check("Index: FR count=2", index["fr_count"] == 2)
check("Index: API count=1", index["api_count"] == 1)

# Duplicate FR ids
dup_fr = copy.deepcopy(valid)
dup_fr["spec"]["functional_requirements"].append({"id": "FR-1", "description": "duplicate"})
errs = validate_spec_strict(dup_fr)
check("Duplicate FR ids detected", any("重复" in e for e in errs), f"errors: {errs}")

# ===== C.2: spec_parser.py =====
print()
print("=== C.2: spec_parser.py ===")
from services.spec_parser import (
    _extract_yaml_block, parse_spec_yaml, parse_spec_from_file,
    format_validation_errors, _try_fix_common_yaml_errors,
)

# Extract YAML from Agent output
agent_output = '''
Let me create a spec for user management.

```yaml
spec:
  name: "User Management"
  version: 1
  functional_requirements:
    - id: FR-1
      description: "User registration"
      acceptance: "POST /register -> 201 + JWT"
    - id: FR-2
      description: "User login"
  constraints:
    - "bcrypt hash passwords"
  files_to_create:
    - "src/users.py"
```

Done!
'''
extracted = _extract_yaml_block(agent_output)
check("YAML extraction from fenced block", extracted.strip().startswith("spec:"),
      f"starts with: {extracted[:30]}")

# Full parse
result = parse_spec_yaml(agent_output)
check("parse_spec_yaml ok=True", result["ok"], f"errors: {result['errors']}")
check("parse_spec_yaml index has FR=2", result["index"].get("fr_count") == 2,
      f"index: {result['index']}")

# Invalid parse
bad = '''spec:
  name: "Bad"
  version: 0
  functional_requirements: []
'''
result2 = parse_spec_yaml(bad)
check("Invalid spec ok=False", not result2["ok"])
check("Invalid spec has >=2 errors", len(result2["errors"]) >= 2,
      f"got {len(result2['errors'])}: {result2['errors']}")

# format_validation_errors
formatted = format_validation_errors(result2["errors"])
check("format_validation_errors non-empty", len(formatted) > 50)

# Auto-fix tabs
bad_yaml = "spec:\n\tname: Test\n\tversion: 1"
fixed = _try_fix_common_yaml_errors(bad_yaml)
check("YAML auto-fix: tabs removed", "\t" not in fixed)

# parse_spec_from_file non-existent
result3 = parse_spec_from_file("/nonexistent/path.yaml")
check("parse_spec_from_file missing: ok=False", not result3["ok"])
check("parse_spec_from_file error message", "不存在" in result3["errors"][0])

# ===== C.3-C.5: spec_prompts.py =====
print()
print("=== C.3-C.5: spec_prompts.py ===")
from prompts.spec_prompts import (
    SPEC_ARCHITECT_PROMPT, SPEC_DEVELOPER_PROMPT, SPEC_AUDITOR_PROMPT,
    GHOST_AUDIT_PROMPT, format_spec_field_descriptions,
    render_architect_prompt, render_developer_prompt, render_auditor_prompt,
)

check("SPEC_ARCHITECT_PROMPT non-empty", len(SPEC_ARCHITECT_PROMPT) > 500)
check("SPEC_ARCHITECT_PROMPT has {task_input}", "{task_input}" in SPEC_ARCHITECT_PROMPT)
check("SPEC_ARCHITECT_PROMPT has {project_structure}", "{project_structure}" in SPEC_ARCHITECT_PROMPT)
check("SPEC_ARCHITECT_PROMPT has {field_descriptions}", "{field_descriptions}" in SPEC_ARCHITECT_PROMPT)

check("SPEC_DEVELOPER_PROMPT non-empty", len(SPEC_DEVELOPER_PROMPT) > 300)
check("SPEC_DEVELOPER_PROMPT has {spec_yaml}", "{spec_yaml}" in SPEC_DEVELOPER_PROMPT)
check("SPEC_DEVELOPER_PROMPT mentions YAGNI", "YAGNI" in SPEC_DEVELOPER_PROMPT)
check("SPEC_DEVELOPER_PROMPT mentions write_file", "write_file" in SPEC_DEVELOPER_PROMPT)

check("SPEC_AUDITOR_PROMPT non-empty", len(SPEC_AUDITOR_PROMPT) > 500)
check("SPEC_AUDITOR_PROMPT has [MATCH]/[MISMATCH]/[MISSING]",
      "[MATCH]" in SPEC_AUDITOR_PROMPT and "[MISMATCH]" in SPEC_AUDITOR_PROMPT and "[MISSING]" in SPEC_AUDITOR_PROMPT)
check("SPEC_AUDITOR_PROMPT has [PASS]/[FAIL]", "[PASS]" in SPEC_AUDITOR_PROMPT and "[FAIL]" in SPEC_AUDITOR_PROMPT)

check("GHOST_AUDIT_PROMPT non-empty", len(GHOST_AUDIT_PROMPT) > 200)
check("GHOST_AUDIT_PROMPT has [GHOST_PASS]/[GHOST_FAIL]",
      "[GHOST_PASS]" in GHOST_AUDIT_PROMPT and "[GHOST_FAIL]" in GHOST_AUDIT_PROMPT)

# render functions
field_desc = format_spec_field_descriptions()
check("format_spec_field_descriptions non-empty", len(field_desc) > 100)

arch_prompt = render_architect_prompt("Build a login system", "src-python/ (15 .py files)")
check("render_architect_prompt resolves placeholders", "{task_input}" not in arch_prompt)
check("render_architect_prompt contains task", "Build a login system" in arch_prompt)
check("render_architect_prompt contains project", "15 .py files" in arch_prompt)

dev_prompt = render_developer_prompt("spec:\n  name: Test\n  version: 1\n  functional_requirements: []")
check("render_developer_prompt resolves {spec_yaml}", "{spec_yaml}" not in dev_prompt)

dev_override = render_developer_prompt("...", override_prompt="FIX MODE: fix these errors")
check("render_developer_prompt override_prompt works", "FIX MODE" in dev_override)

audit_prompt = render_auditor_prompt("spec_yaml_here", "created 3 files", "previous round: 2 mismatches")
check("render_auditor_prompt resolves placeholders", "{spec_yaml}" not in audit_prompt)
check("render_auditor_prompt has implementation", "created 3 files" in audit_prompt)
check("render_auditor_prompt has additional_ctx", "previous round: 2 mismatches" in audit_prompt)

# ===== Result =====
print()
print(f"{'='*40}")
print(f"Total: {passes} passed, {fails} failed out of {passes+fails}")
if fails == 0:
    print("ALL CHECKS PASSED")
else:
    print(f"{fails} FAILURES — fix before continuing")
    sys.exit(1)
