---
id: eval-react-rerender
title: Stopping a React list from re-rendering every item on each keystroke
kind: note
origin: personal
date: "2026-02-07"
tags:
  - react
  - frontend
  - performance
tools:
  - react
  - react-devtools
concepts:
  - memoization
  - referential stability
  - render profiling
relates_to: []
summary: A search box re-rendered the whole list on every keystroke; a stable callback plus memoized rows cut renders to only the changed item.
---

Typing in a search box made a thousand-row list visibly lag. The React DevTools
profiler showed that every list item re-rendered on each keystroke, even rows whose
data had not changed.

The cause was a fresh `onSelect` arrow function created inline on every parent
render, which broke referential equality and defeated any child memoization. The fix
was to wrap the handler in `useCallback` so its identity is stable, and wrap each row
component in `React.memo` so it only re-renders when its own props change.

After that the profiler showed only the actively edited row re-rendering, and typing
was smooth again. The rule: memoization is useless unless the props you pass are
referentially stable across renders.
