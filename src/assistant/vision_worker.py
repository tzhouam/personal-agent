"""One-shot local VLM subprocess: JSON job on stdin → JSON result on stdout.

Run as `python -m assistant.vision_worker`. Input:
`{"model_path": …, "images": [paths…], "prompt": …, "min_free_gib": 20}`;
output: `{"descriptions": [one string per image, in order]}`.

Kept as a subprocess (not an import in the daemon) deliberately: the model
loads onto whichever CUDA device currently has the most free memory, answers,
and the process exits — so the daemon never holds ~18 GB of GPU between
images on machines where the GPUs are shared with other jobs. Diagnostics go
to stderr; stdout carries only the result JSON.
"""

import json
import sys


def _pick_device(min_free_gib: float) -> str:
    """CUDA device with the most free memory, requiring at least
    `min_free_gib` free (the 8B bf16 model + activations need ~20 GiB).
    Raises RuntimeError when no device qualifies — mid-run CI jobs may have
    taken everything."""
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError("no CUDA device available")
    best, best_free = None, 0.0
    for i in range(torch.cuda.device_count()):
        free_gib = torch.cuda.mem_get_info(i)[0] / 2**30
        if free_gib > best_free:
            best, best_free = i, free_gib
    if best is None or best_free < min_free_gib:
        raise RuntimeError(
            f"no GPU with {min_free_gib} GiB free (best: {best_free:.0f} GiB)")
    return f"cuda:{best}"


def main() -> int:
    """Read the job, load the model once, describe every image, print JSON.
    Per-image failures become bracketed error strings so one bad image never
    costs the batch; only setup failures exit non-zero."""
    job = json.loads(sys.stdin.read())
    device = _pick_device(float(job.get("min_free_gib", 20)))
    print(f"vision worker: loading {job['model_path']} on {device}", file=sys.stderr)

    import torch
    from transformers import AutoModelForImageTextToText, AutoProcessor

    model = AutoModelForImageTextToText.from_pretrained(
        job["model_path"], dtype=torch.bfloat16, device_map=device)
    processor = AutoProcessor.from_pretrained(job["model_path"])

    descriptions = []
    for path in job["images"]:
        try:
            messages = [{"role": "user", "content": [
                {"type": "image", "image": path},
                {"type": "text", "text": job["prompt"]},
            ]}]
            inputs = processor.apply_chat_template(
                messages, add_generation_prompt=True, tokenize=True,
                return_dict=True, return_tensors="pt").to(device)
            with torch.inference_mode():
                out = model.generate(**inputs, max_new_tokens=600, do_sample=False)
            text = processor.batch_decode(
                out[:, inputs["input_ids"].shape[1]:], skip_special_tokens=True)[0]
            descriptions.append(text.strip())
        except Exception as exc:  # one bad image must not cost the batch
            print(f"vision worker: {path} failed: {exc}", file=sys.stderr)
            descriptions.append(f"[image could not be analyzed: {exc}]")
    json.dump({"descriptions": descriptions}, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
