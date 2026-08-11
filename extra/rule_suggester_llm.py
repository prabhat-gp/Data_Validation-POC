"""
rule_suggester_llm.py
---------------------
AI-powered rule suggestion using locally-running Ollama (Llama 3.2).

How it works:
  1. Query schema + sample data from source_db
  2. Send to local Ollama/Llama 3.2 with structured JSON prompt
  3. Parse LLM response (with error handling for hallucinations/malformed JSON)
  4. Display suggestions + review loop before insertion

Usage:
    python rule_suggester_llm.py --table b2bcustomer
    python rule_suggester_llm.py --table b2bproduct --auto-insert
"""

import json
import re
import sys
import argparse
import pandas as pd
from typing import List, Dict, Optional
from datetime import datetime

try:
    import ollama
except ImportError:
    print("ERROR: ollama package not installed. Run: pip install ollama")
    sys.exit(1)

from db import get_engine

# ===== CONFIG =====
OLLAMA_MODEL = "llama3.2"
OLLAMA_HOST = "http://localhost:11434"

# Mapping of table name to primary key column
PK_MAP = {
    "b2bcustomer": "customer_id",
    "b2bsbg": "sbg_id",
    "b2bproduct": "product_id",
    "b2bprice": "price_id",
}


def check_ollama_connection():
    """Verify Ollama service is running and model is available."""
    try:
        # Try a simple ping to the Ollama service
        response = ollama.list()
        print(f"✓ Ollama service is running")
        
        # Check if model exists - handle ListResponse object from ollama package
        models = []
        if hasattr(response, "models"):
            # ollama._types.ListResponse object with Model objects
            for m in response.models:
                # Each m is a Model object with a 'model' attribute
                if hasattr(m, "model"):
                    models.append(m.model)
        elif isinstance(response, dict) and "models" in response:
            # Fallback for dict format: {"models": [...]}
            for m in response.get("models", []):
                if isinstance(m, dict):
                    model_name = m.get("name") or m.get("model") or str(m)
                    models.append(model_name)
        
        # Check if the model is available (handle version tags like llama3.2:latest)
        model_found = False
        for m in models:
            if OLLAMA_MODEL in m or m.startswith(OLLAMA_MODEL):
                model_found = True
                break
        
        if model_found:
            print(f"✓ Model '{OLLAMA_MODEL}' is available")
            return True
        else:
            print(f"⚠ Model '{OLLAMA_MODEL}' not found locally.")
            print(f"  Available models: {models if models else 'none'}")
            print(f"\n  To download, run in a regular terminal (not venv):")
            print(f"    ollama pull {OLLAMA_MODEL}")
            print(f"\n  Or use a smaller model if on limited resources:")
            print(f"    ollama pull llama3.2:1b")
            return False
    except Exception as e:
        print(f"✗ Could not connect to Ollama: {e}")
        print(f"  Make sure Ollama is running (check System Tray > Ollama)")
        return False


def get_table_schema_and_sample(table_name: str) -> tuple:
    """Fetch column schema and sample rows from source_db."""
    source_engine = get_engine("source")
    
    # Get schema
    schema_query = """
        SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_KEY
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = 'source_db'
        AND TABLE_NAME = %s
        ORDER BY ORDINAL_POSITION
    """
    
    with source_engine.connect() as conn:
        schema_df = pd.read_sql(schema_query, conn, params=(table_name,))
        sample_df = pd.read_sql(f"SELECT * FROM {table_name} LIMIT 20", conn)
    
    return schema_df, sample_df


def build_prompt(table_name: str, schema_df: pd.DataFrame, sample_df: pd.DataFrame) -> str:
    """Build the LLM prompt for rule suggestion."""
    schema_str = schema_df.to_string(index=False)
    sample_str = sample_df.head(10).to_string(index=False)  # Show only first 10 rows in prompt
    
    prompt = f"""You are a data quality expert specializing in B2B data validation.

Given this table schema and sample data, suggest validation rules for each column.
Focus on practical, actionable rules that would catch real data quality issues.

TABLE: {table_name}
SCHEMA:
{schema_str}

SAMPLE DATA (first 10 rows):
{sample_str}

---

For each column, suggest ONE primary rule if applicable. Return ONLY valid JSON, no other text.

JSON format (array of rule suggestions):
{{
  "rules": [
    {{
      "column_name": "email",
      "rule_type": "format",
      "severity": "critical",
      "fix_action": "manual",
      "parameters": {{"regex": "^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{{2,}}$"}},
      "reason": "Email should match standard format"
    }},
    {{
      "column_name": "status",
      "rule_type": "allowed_values",
      "severity": "warning",
      "fix_action": "manual",
      "parameters": {{"values": ["Active", "Inactive", "Pending"]}},
      "reason": "Status should be one of the allowed values"
    }},
    {{
      "column_name": "customer_name",
      "rule_type": "trim",
      "severity": "info",
      "fix_action": "auto",
      "parameters": {{"trim": true}},
      "reason": "Name fields should have leading/trailing whitespace removed"
    }}
  ]
}}

IMPORTANT:
- Only suggest rules that make sense for this specific table and column
- For text columns with obvious formatting needs (names, codes), suggest trim or case normalization
- For columns with clear patterns (email, phone, code format), suggest format rules with realistic regex
- For low-cardinality columns (status, region), suggest allowed_values from the sample data
- For numeric columns, check sample values and suggest range rules if appropriate
- For nullable columns, consider null_check rules
- Return valid JSON only. Do not add explanations outside the JSON.
"""
    return prompt


def parse_llm_response(response_text: str) -> Optional[List[Dict]]:
    """Extract and parse JSON from LLM response, with fallback for hallucinations."""
    # Try direct JSON parsing first
    try:
        data = json.loads(response_text)
        if isinstance(data, dict) and "rules" in data:
            return data["rules"]
    except json.JSONDecodeError:
        pass
    
    # Fallback: extract JSON from markdown code blocks
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response_text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            if isinstance(data, dict) and "rules" in data:
                return data["rules"]
        except json.JSONDecodeError:
            pass
    
    # Last resort: try to find a valid JSON object in the text
    start_idx = response_text.find("{")
    if start_idx != -1:
        # Find matching closing brace
        depth = 0
        for i, char in enumerate(response_text[start_idx:], start=start_idx):
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(response_text[start_idx:i+1])
                        if isinstance(data, dict) and "rules" in data:
                            return data["rules"]
                    except json.JSONDecodeError:
                        pass
                    break
    
    return None


def suggest_rules_for_table(table_name: str) -> Optional[List[Dict]]:
    """Main flow: fetch schema, call LLM, parse response."""
    print(f"\n{'='*60}")
    print(f"Suggesting rules for table: {table_name}")
    print(f"{'='*60}")
    
    # Get schema and sample
    print("Fetching schema and sample data...")
    try:
        schema_df, sample_df = get_table_schema_and_sample(table_name)
        print(f"  ✓ Loaded {len(schema_df)} columns, {len(sample_df)} sample rows")
    except Exception as e:
        print(f"  ✗ Error fetching data: {e}")
        return None
    
    # Build prompt
    prompt = build_prompt(table_name, schema_df, sample_df)
    
    # Call LLM
    print(f"\nCalling {OLLAMA_MODEL}...")
    try:
        response = ollama.chat(
            model=OLLAMA_MODEL,
            messages=[{"role": "user", "content": prompt}],
            stream=False
        )
        response_text = response["message"]["content"]
    except Exception as e:
        print(f"  ✗ Error calling Ollama: {e}")
        return None
    
    # Parse response
    print("Parsing LLM response...")
    rules = parse_llm_response(response_text)
    
    if rules is None:
        print(f"  ✗ Could not parse valid JSON from LLM response")
        print(f"\nRaw response:\n{response_text}")
        return None
    
    print(f"  ✓ Parsed {len(rules)} rule suggestion(s)")
    return rules


def display_rules_for_review(table_name: str, rules: List[Dict]) -> None:
    """Pretty-print rules for user review."""
    print(f"\n{'='*60}")
    print(f"SUGGESTED RULES FOR {table_name.upper()}")
    print(f"{'='*60}\n")
    
    for i, rule in enumerate(rules, 1):
        print(f"{i}. Column: {rule.get('column_name', 'UNKNOWN')}")
        print(f"   Type: {rule.get('rule_type', '?')}")
        print(f"   Severity: {rule.get('severity', '?')}")
        print(f"   Fix Action: {rule.get('fix_action', '?')}")
        print(f"   Reason: {rule.get('reason', '(no reason provided)')}")
        if rule.get("parameters"):
            print(f"   Parameters: {json.dumps(rule['parameters'], indent=14)}")
        print()


def prompt_for_approval(rules: List[Dict]) -> bool:
    """Ask user if they want to proceed with inserting these rules."""
    response = input("\nDo you want to insert these rules into validation_rules? (yes/no): ").strip().lower()
    return response in ("yes", "y")


def insert_rules_into_db(table_name: str, rules: List[Dict]) -> int:
    """Insert approved rules into config_db.validation_rules."""
    config_engine = get_engine("config")
    
    rows_to_insert = []
    for rule in rules:
        # Validate required fields
        if not rule.get("column_name") or not rule.get("rule_type"):
            print(f"  ⚠ Skipping incomplete rule: {rule}")
            continue
        
        row = {
            "rule_name": f"{rule.get('column_name')} {rule.get('rule_type')}",
            "table_name": table_name,
            "column_name": rule.get("column_name"),
            "rule_type": rule.get("rule_type"),
            "severity": rule.get("severity", "warning"),
            "parameters": json.dumps(rule.get("parameters", {})) if rule.get("parameters") else None,
            "fix_action": rule.get("fix_action", "manual"),
            "fix_logic": None,  # LLM doesn't determine this yet
            "is_active": 1,
        }
        rows_to_insert.append(row)
    
    if not rows_to_insert:
        print("  ✗ No valid rules to insert")
        return 0
    
    # Insert
    try:
        df = pd.DataFrame(rows_to_insert)
        with config_engine.begin() as conn:
            df.to_sql("validation_rules", conn, if_exists="append", index=False)
        print(f"  ✓ Inserted {len(rows_to_insert)} rule(s)")
        return len(rows_to_insert)
    except Exception as e:
        print(f"  ✗ Error inserting rules: {e}")
        return 0


def main():
    parser = argparse.ArgumentParser(
        description="Suggest validation rules using local LLM (Ollama)"
    )
    parser.add_argument(
        "--table",
        required=True,
        choices=list(PK_MAP.keys()),
        help="Table name to suggest rules for"
    )
    parser.add_argument(
        "--auto-insert",
        action="store_true",
        help="Skip review and auto-insert rules"
    )
    
    args = parser.parse_args()
    table_name = args.table
    
    # Check Ollama connection
    if not check_ollama_connection():
        sys.exit(1)
    
    # Get suggestions
    rules = suggest_rules_for_table(table_name)
    if rules is None:
        sys.exit(1)
    
    # Display for review
    display_rules_for_review(table_name, rules)
    
    # Ask for approval (unless auto-insert)
    if not args.auto_insert:
        if not prompt_for_approval(rules):
            print("Aborted.")
            sys.exit(0)
    else:
        print(f"Auto-inserting {len(rules)} rules...")
    
    # Insert
    print(f"\nInserting rules into validation_rules...")
    inserted = insert_rules_into_db(table_name, rules)
    
    if inserted > 0:
        print(f"\n✓ Successfully inserted {inserted} rule(s)")
        print(f"\nNext step: Run 'python engine.py' to validate using the new rules")
    else:
        print(f"\n✗ No rules were inserted")
        sys.exit(1)


if __name__ == "__main__":
    main()
