/-! Statements a human froze. An agent proves them in `Solution.lean`; it may not change them. -/

/-- The total of a cart, as `examples/python-cli` computes it. -/
def total : List Int → Int
  | [] => 0
  | x :: xs => x + total xs

/-- Totalling two carts separately and adding agrees with totalling them together. -/
theorem total_append (xs ys : List Int) : total (xs ++ ys) = total xs + total ys := sorry

/-- Reordering a cart's two halves does not change its total. -/
theorem total_swap (xs ys : List Int) : total (xs ++ ys) = total (ys ++ xs) := sorry
