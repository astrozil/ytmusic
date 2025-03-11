from transformers import AutoTokenizer, AutoModelForCausalLM
import torch

# Load a free model (GPT-Neo 125M is a lightweight, free option)
tokenizer = AutoTokenizer.from_pretrained("EleutherAI/gpt-neo-125M")
model = AutoModelForCausalLM.from_pretrained("EleutherAI/gpt-neo-125M")

prompt = "Provide a list of 10 random but famous music artists from around the world:\n"
input_ids = tokenizer.encode(prompt, return_tensors="pt")

# Generate text using sampling
output = model.generate(
    input_ids,
    max_length=100,
    do_sample=True,
    temperature=0.7,
    top_p=0.9,
    num_return_sequences=1
)
result = tokenizer.decode(output[0], skip_special_tokens=True)
print(result)
