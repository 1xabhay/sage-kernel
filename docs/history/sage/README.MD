# Sage Kernel

> **Historical.** The old kernel, before the rewrite of 2026-10-20. Its `make` commands were that
> repository's and do not run here. The current kernel is described in the [root README](../../../README.md).

Sage is a minimal, self-hosted, model-independent runtime for a persistent
personal agent. The first experiment is one agent shared by the browser and
Reachy Mini. The [engineering charter](KERNEL-CHARTER.md) defines that boundary.

The browser connects to a shared kernel with an injected decision provider and
a SQLite journal. Events and requested effects are committed before dispatch;
known turn outcomes survive restart. Interrupted replies remain unknown until
acknowledged and are never retried automatically. Browser → restart → mock-device
recall and device-disconnect isolation are tested. Live Reachy integration is next.

Run the companion locally:

```sh
make up
make check
```

Open [Sage at localhost:8765](http://127.0.0.1:8765/). Type to the character,
optionally enable spoken replies, and use Stop. Without a configured model,
the page is explicitly a preview with authored replies.

Read [the fundamentals and current contract](FUNDAMENTALS.md) for the
minimum loop, ownership, real-model configuration, and implementation limits.
Run `make help` for tests, status, logs, snapshots, builds, packaging, and the
future SSH tunnel. `make down` preserves companion state.

The previous OpenClaw distribution experiment remains in
`compose.reference.yaml`, accessed through `make reference-up` and
`make reference-check`. Its state and backups remain separate. Earlier plans
and research are preserved as historical reference; the owner's correction
replaces their selected-runtime decision. See [execution evidence](EVIDENCE.md).

Run the dependency-free policy tests from this directory:

```sh
python3 -m unittest discover -s tests -v
```

The core requests effects; the shell executes them and reports outcomes.
The philosophical notes below remain the broader project motivation.

What does it mean for an AI to act as a first person ?

## What do I mean by first person ?

- A software model to interact with real world?
- Emulate human observation and communication?

## How does a software model interact with real world?

- Act as electronic money movement operator?
- electronic payment is foundational infrastructure supporting this?
- Join conversation over socials, communication channels?
- Whould it be wierd to say: Act as electronic thought movement operator?
- Are conversations an exchange of thought?

## Emulate human observation and communication?

- New Spatial Undersating of real world?
- New Language Understanding of humman languages?
- The non deterministic nature of a software can be measured?

## when is a software called non determinitc?

- are neural network architectures inherrently non deterministic?

## what does it mean to be non deterministic?

- what are newural network architecures?

### non-deterministic in mathematics, computing according to cambridge dictionary

> producing a different result each time the same piece of data is entered:
>
> - The calculus is non-deterministic because the substitution mechanism does not follow any strategy for fetching resources from arguments.
> - In the theory of computation, a nondeterministic finite state machine is one where there may be several possible next states.

## what google gemini says?

---

### 1. Training is Governed by Stochastic Processes

Training is mathematically non-deterministic because it relies on stochastic optimization and probability distributions.

- **Stochastic Gradient Descent (SGD):** Instead of calculating the true gradient over the entire dataset D, SGD approximates it using a random mini-batch B.

  $$\nabla_{\theta} L(\theta) \approx \frac{1}{|B|} \sum_{x \in B} \nabla_{\theta} \ell(x, \theta)$$

  Because B is sampled randomly, the optimization path is a stochastic process (specifically, a Markov chain).

- **Dropout Regularization:** During training, each neuron is deactivated independently with a probability p drawn from a Bernoulli distribution:

  $$r \sim \text{Bernoulli}(1-p)$$

  $$h_{\text{dropped}} = r \odot h$$

  Every training forward pass uses a completely different random matrix, changing the underlying mathematical function being evaluated.

### 2. Standard Inference is a Fixed Composite Function

Once a standard network (like a ResNet or a basic MLP) is trained, all randomness is removed. Mathematically, the model becomes a deterministic composite function.

If $f_l$ represents the operation of layer l (combining matrix multiplication and a non-linear activation function σ), the entire network is just a sequence of fixed mathematical operations:

$$F(x) = f_L(f_{L-1}(\dots f_1(x) \dots))$$

Where each layer is defined by fixed weights W and biases b:

$$f_l(h) = \sigma(W_l h + b_l)$$

Because W, b, and σ are constants, the mapping from input x to output y is a strict mathematical function: for every x, there exists exactly one y.

### 3. Generative Inference Uses Categorical Sampling

When an LLM or generative model outputs text, it doesn't just output a word; it outputs a probability distribution over a vocabulary V using the Softmax function. For a logit vector z, the probability of the next token i is:

$$P(\text{token}_i) = \frac{e^{z_i / T}}{\sum_j e^{z_j / T}}$$

- **The Role of Temperature (T):** The temperature parameter scales the logits before the Softmax. As T → 0, the distribution concentrates entirely on the highest logit (argmax), making it a deterministic selection.
- **The Sampling Step:** When T > 0, the model doesn't just pick the top choice. It samples a token from this Categorical Distribution. The actual selection relies on a pseudo-random number generator (PRNG) algorithm (like Mersenne Twister), introducing true algorithmic randomness.

### 4. The Hardware Catch: Non-Associative Algebra

Even if you set T = 0 or run standard inference, computers can still introduce non-determinism due to floating-point arithmetic.

In mathematics, addition is associative: (a + b) + c = a + (b + c).

In computer science, floating-point addition is not associative because of rounding errors:

$$(a+b)+c \ne a+(b+c)$$

When a GPU performs massive parallel matrix multiplications, the exact order in which billions of numbers are added up (the "reduction order") can change dynamically based on thread scheduling and hardware temperature. Because the order changes, the tiny rounding errors change, which can occasionally flip the final output token of an LLM.

---


what is the range of generative inference by categorical sampling ? or is it random ?
