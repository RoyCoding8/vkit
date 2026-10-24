From refinedrust.examples.vkit_qualification.generated Require generated_code_vkit_qualification.
From radium Require Import lang notation.
From refinedrust Require Import typing shims.

Set Default Proof Using "Type".

Definition foundation_proof_irrelevance :
  ∀ (P : Prop) (p1 p2 : P), p1 = p2 :=
  Stdlib.Logic.ProofIrrelevance.proof_irrelevance.

Definition foundation_functional_extensionality :
  ∀ (A : Type) (B : A → Type) (f g : ∀ x : A, B x),
    (∀ x : A, f x = g x) → f = g :=
  @Stdlib.Logic.FunctionalExtensionality.functional_extensionality_dep.

Definition reserve_result (used capacity amount : Z) : Z :=
  if decide (0 < amount ∧ amount ≤ capacity - used) then used + amount else used.

Section contract.
Context `{RRGS : !refinedrustGS Σ}.

Definition expected_traits : spec_with _ _ (thread_id → iProp Σ) :=
  spec! ( *[]) : 0 | ( *[]) : ([] : list RT), (λ π, (True)%I).

Definition expected_spec :=
  fn(∀ ( *[]) : 0 | ( *[]) : ([] : list (RT * syn_type)%type) |
      (u, c, a) : (Z * Z * Z),
      (λ ϝ, []);
      u :@: (int U32), c :@: (int U32), a :@: (int U32);
      (λ π : thread_id, ⌜(0 ≤ u ≤ c)%Z⌝) |
      (λ π : thread_id, True)) →
      ∃ _ : unit, reserve_result u c a @ (int U32);
      (λ π : thread_id, True) |
      False |
      (λ π : thread_id, True).

Definition expected_body (π : thread_id) : Prop :=
  ∀ panic_shim_loc : loc,
  syn_type_is_layoutable (tuple2_sls (IntSynType U32) BoolSynType : syn_type) →
  panic_shim_loc ◁ᵥ{π, MetaNone} panic_shim_loc @ function_ptr []
    (<tag_type> spec! ( *[]) : 0 | ( *[]) : ([] : list RT),
      fn_spec_add_late_pre (type_of_panic_shim (RRGS:=RRGS) -[] -[]) (λ π, True)%I) -∗
  refinedrust.primitive.functions.typed_function π
    (refinedrust.examples.vkit_qualification.generated.generated_code_vkit_qualification.reserve_def panic_shim_loc)
    (<tag_type> spec! ( *[]) : 0 | ( *[]) : ([] : list RT),
      fn_spec_add_late_pre (expected_spec -[] -[])
        (λ π, True ∗ ((expected_traits -[] -[]) π))%I).

End contract.
