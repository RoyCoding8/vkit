def total : List Int → Int
  | [] => 0
  | x :: xs => x + total xs

theorem total_append (xs ys : List Int) : total (xs ++ ys) = total xs + total ys := by
  induction xs with
  | nil => simp [total]
  | cons x xs ih => simp [total, ih, Int.add_assoc]

theorem total_swap (xs ys : List Int) : total (xs ++ ys) = total (ys ++ xs) := by
  rw [total_append, total_append, Int.add_comm]
