import json
import os

def combine_datasets(input_files, output_file):
    """Combine multiple datasets into one"""
    combined_samples = []
    
    for input_file in input_files:
        if os.path.exists(input_file):
            with open(input_file, 'r', encoding='utf-8') as f:
                samples = json.load(f)
                combined_samples.extend(samples)
            print(f"Loaded {len(samples)} samples from {input_file}")
        else:
            print(f"Warning: File {input_file} not found")
    
    # Save combined dataset
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(combined_samples, f, ensure_ascii=False, indent=2)
    
    print(f"Combined {len(combined_samples)} samples into {output_file}")

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Combine multiple datasets")
    parser.add_argument("--inputs", nargs='+', required=True, help="Input JSON files")
    parser.add_argument("--output", required=True, help="Output JSON file")
    
    args = parser.parse_args()
    combine_datasets(args.inputs, args.output)
