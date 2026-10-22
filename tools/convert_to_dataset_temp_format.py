import json
import os
from pathlib import Path

def convert_to_dataset_temp_format(input_file, output_file):
    """Convert processed dataset to dataset_temp format"""
    # Read input file
    with open(input_file, 'r', encoding='utf-8') as f:
        samples = json.load(f)
    
    # Convert samples
    converted_samples = []
    for sample in samples:
        # Extract code
        buggy_code = sample.get('code', {}).get('buggy', '')
        fixed_code = sample.get('code', {}).get('fixed', '')
        
        # Extract issue information
        issue = sample.get('issue', {})
        category = issue.get('category', '')
        sub_category = issue.get('sub_category', '')
        severity = issue.get('severity', 'medium')
        description = issue.get('description', '')
        
        # Create converted sample
        converted = {
            "id": sample.get('code_id', sample.get('id', '')),
            "language": sample.get('language', 'python'),
            "code": buggy_code,
            "fixed_code": fixed_code,
            "issues": [
                {
                    "category": category,
                    "subtype": sub_category,
                    "severity": severity,
                    "description": description,
                    "location": {
                        "line_start": 1,
                        "line_end": buggy_code.count('\n') + 1
                    }
                }
            ]
        }
        converted_samples.append(converted)
    
    # Save output file
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(converted_samples, f, ensure_ascii=False, indent=2)
    
    print(f"Converted {len(converted_samples)} samples to dataset_temp format")
    print(f"Saved to {output_file}")

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Convert processed dataset to dataset_temp format")
    parser.add_argument("--input", required=True, help="Input JSON file")
    parser.add_argument("--output", required=True, help="Output JSON file")
    
    args = parser.parse_args()
    convert_to_dataset_temp_format(args.input, args.output)
