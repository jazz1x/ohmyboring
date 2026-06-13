# My Philosophy — The Designer of Integrity

> What I pursue is not a language but **integrity**.
> *Wholeness* (parts not contradicting the whole) + *honesty* (the surface matching the inside).
> Rust is merely the best tool currently available for having the compiler enforce this; I use it
> not because it is a tool, but because its bones and mine are the same.

---

## In One Sentence

> **I can't stand state that lies. So I structure things so that lying becomes impossible.**

ADT, Parse Don't Validate, fail-fast, ROP, SRP, linearity, dependency inversion, boundaries,
"the type is the proof," thin design — all of these are variations on that one sentence. It splits into three layers.

---

## Layer 1. Epistemology — The Representation Must Be True

Code must represent the world honestly. Drive the gap between representation and reality to zero.

- A `String` holding an email is a lie — because it can also hold things that aren't emails.
- An `Email` type is the truth — because it can only hold emails.
- **Good design closes off the paths that can be wrong** (the code-level counterpart of falsifiability).

### Criteria
- Can this type hold a value it **should not** be able to hold? → If so, the type is lying.
- Do combinations of bool flags create "impossible states"? → Close them off with an enum.
- Can you tell what is guaranteed just by looking at the signature? → If not, the type is weak.

→ **ADT · Parse Don't Validate · "the type is the proof"** come from here.

---

## Layer 2. Aesthetics — The Flow Must Go One Way

Data enters at the boundary, flows in a single direction, and on failure immediately drops onto the side track,
never to return. Just as time doesn't flow backward, data should not flow backward either.

This is resistance to entropy. Left unattended, dependencies tangle and state mutates from all directions.
You dig a **channel** in the river, not a dam.

### Criteria
- Does data flow in one direction, top → bottom? Is there anywhere it leaks backward or sideways?
- Does failure drop onto the side track (`Err`) without polluting the main flow?
- Does invalid input cross the boundary and make it all the way inside? → Reject it at the door with fail-fast.
- Is there a cyclic dependency? → Point the direction one way with dependency inversion (a trait).

→ **Linearity · ROP · fail-fast · dependency inversion** come from here.

---

## Layer 3. Ethics — Guarantee the Most with the Least Intervention

Not piling on more, but **blocking the most lies with the least structure.**

A type zealot covers everything in types (a nuclear bunker).
The designer of integrity looks for **where placing a single layer guarantees all the rest**.
Parsing at one boundary makes the entire inside safe — the high-leverage, minimal point of intervention.
This is the definition of elegance — "the state where there is nothing more to remove."

### Criteria
- Is this abstraction really needed? Does a simpler representation guarantee the same invariant? (First principles)
- If I don't build it now, who will miss it? If nobody, it's a nuclear bunker. Defer it.
- Am I building with triple-nested generics what a single trait layer would do?
- Am I abstracting prematurely out of fear of duplication? → rule of three. Start worrying at the third one.

→ **Thin design · SRP · restraint · the nuclear-bunker boundary** come from here.

---

## Priority When They Conflict

Principles will inevitably conflict. When they do, judge in this order:

1. **Honesty comes first (Layer 1).** A type that lies is out, however fast or elegant.
2. **Then flow (Layer 2).** If it's honest, prefer the side that flows in one direction.
3. **Restraint is last (Layer 3).** If it's honest and one-directional, prefer less structure.

**The higher layer beats the lower layer.**

> Example: error accumulation (Validated) violates fail-fast (Layer 2).
> But at a UX boundary, "honestly showing the user every failure" (Layer 1) takes priority,
> so it is justified. Layer 1 > Layer 2.

---

## Beyond the Language

These three layers keep working even if Rust disappears.

- When I write prose, I can't stand a claim and its evidence not lining up (Layer 1).
- When I build a team, I can't stand responsibility flowing in all directions (Layer 2).
- Whatever I do, I'm ashamed to paper it over with excessive procedure (Layer 3).

**"Rust developer" can fade, but "designer of integrity" does not.**
The former is a job; the latter is a way of seeing. A way of seeing is something no tool can take away.

---

## When Stuck

> **"Does this code lie?"**

Come back to this one question. If no, it passes. If in doubt, make it simpler.
For the concrete how-to → `RUST-STYLE.md`
