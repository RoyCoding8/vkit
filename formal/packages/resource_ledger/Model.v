From stdpp Require Import gmap.
From Stdlib Require Import Lia.

Local Open Scope nat_scope.

Inductive reserve_error :=
  | ZeroAmount
  | DuplicateId
  | NoRecordSpace
  | InsufficientCapacity.

Inductive release_error :=
  | MissingId
  | WrongOwner.

Inductive batch_error :=
  | BatchTooLarge
  | BatchFailure (index : nat) (error : reserve_error).

Section resource_ledger.
Context {Id Owner : Type} `{Countable Id} `{EqDecision Owner}.

Record reservation := {
  reservation_owner : Owner;
  reservation_amount : nat;
}.

Record request := {
  request_owner : Owner;
  request_id : Id;
  request_amount : nat;
}.

Record ledger := {
  capacity : nat;
  max_records : positive;
  reservations : gmap Id reservation;
}.

Definition used (records : gmap Id reservation) : nat :=
  map_fold (fun _ entry total => reservation_amount entry + total) 0 records.

Definition valid (s : ledger) : Prop :=
  (forall id entry, reservations s !! id = Some entry ->
     0 < reservation_amount entry) /\
  used (reservations s) <= capacity s /\
  size (reservations s) <= Pos.to_nat (max_records s).

Definition init (cap : nat) (record_bound : positive) : ledger :=
  {| capacity := cap; max_records := record_bound; reservations := ∅ |}.

Definition with_records (s : ledger) (records : gmap Id reservation) : ledger :=
  {| capacity := capacity s; max_records := max_records s;
     reservations := records |}.

Definition reserve (s : ledger) (r : request) : ledger * (reserve_error + unit) :=
  if decide (request_amount r = 0) then (s, inl ZeroAmount) else
  match reservations s !! request_id r with
  | Some _ => (s, inl DuplicateId)
  | None =>
      if decide (size (reservations s) < Pos.to_nat (max_records s)) then
        if decide (request_amount r <= capacity s - used (reservations s)) then
          (with_records s
             (<[request_id r := {| reservation_owner := request_owner r;
                                  reservation_amount := request_amount r |}]>
                (reservations s)), inr tt)
        else (s, inl InsufficientCapacity)
      else (s, inl NoRecordSpace)
  end.

Definition release (s : ledger) (owner : Owner) (id : Id) :
    ledger * (release_error + nat) :=
  match reservations s !! id with
  | None => (s, inl MissingId)
  | Some entry =>
      if decide (reservation_owner entry = owner) then
        (with_records s (delete id (reservations s)), inr (reservation_amount entry))
      else (s, inl WrongOwner)
  end.

Fixpoint reserve_sequence (s : ledger) (requests : list request) :
    (nat * reserve_error) + ledger :=
  match requests with
  | [] => inr s
  | r :: rest =>
      match reserve s r with
      | (_, inl error) => inl (0, error)
      | (next, inr _) =>
          match reserve_sequence next rest with
          | inl (index, error) => inl (S index, error)
          | inr final => inr final
          end
      end
  end.

Context (batch_bound : positive).

Definition reserve_batch (s : ledger) (requests : list request) :
    ledger * (batch_error + unit) :=
  if decide (length requests <= Pos.to_nat batch_bound) then
    match reserve_sequence s requests with
    | inl (index, error) => (s, inl (BatchFailure index error))
    | inr final => (final, inr tt)
    end
  else (s, inl BatchTooLarge).

Lemma used_empty : used (∅ : gmap Id reservation) = 0.
Proof. apply map_fold_empty. Qed.

Lemma used_insert (records : gmap Id reservation) id entry :
  records !! id = None ->
  used (<[id := entry]> records) = reservation_amount entry + used records.
Proof.
  intros Hfresh. unfold used.
  apply (map_fold_insert_L
    (fun _ entry total => reservation_amount entry + total) 0 id entry records).
  - intros. simpl. lia.
  - exact Hfresh.
Qed.

Lemma used_delete (records : gmap Id reservation) id entry :
  records !! id = Some entry ->
  used records = reservation_amount entry + used (delete id records).
Proof.
  intros Hlookup. unfold used.
  apply (map_fold_delete_L
    (fun _ entry total => reservation_amount entry + total) 0 id entry records).
  - intros. simpl. lia.
  - exact Hlookup.
Qed.

Lemma init_valid cap record_bound : valid (init cap record_bound).
Proof.
  unfold valid. split.
  - intros id entry Hlookup.
    change ((∅ : gmap Id reservation) !! id = Some entry) in Hlookup.
    rewrite lookup_empty in Hlookup. discriminate.
  - split.
    + change (used (∅ : gmap Id reservation) <= cap).
      rewrite used_empty. lia.
    + change (size (∅ : gmap Id reservation) <= Pos.to_nat record_bound).
      rewrite map_size_empty. lia.
Qed.
Lemma reserve_valid (s : ledger) (r : request) :
  valid s -> valid (fst (reserve s r)).
Proof.
  intros Hvalid. unfold reserve.
  destruct (decide (request_amount r = 0)) as [Hzero|Hnonzero]; cbn [fst].
  - exact Hvalid.
  - destruct (reservations s !! request_id r) as [entry|] eqn:Hlookup; cbn [fst].
    + exact Hvalid.
    + destruct (decide (size (reservations s) < Pos.to_nat (max_records s)))
        as [Hroom|Hfull]; cbn [fst].
      * destruct (decide (request_amount r <= capacity s - used (reservations s)))
          as [Hfits|Hinsufficient]; cbn [fst].
        -- destruct Hvalid as [Hpositive [Hused Hsize]].
           cbn [valid with_records reservations capacity max_records]. split.
           ++ intros id entry Hnew. apply lookup_insert_Some in Hnew.
              destruct Hnew as [[-> <-]|[_ Hold]].
              ** cbn. lia.
              ** eapply Hpositive. exact Hold.
           ++ split.
              ** rewrite used_insert by exact Hlookup. lia.
              ** rewrite map_size_insert_None by exact Hlookup. lia.
        -- exact Hvalid.
      * exact Hvalid.
Qed.

Lemma release_valid (s : ledger) owner id :
  valid s -> valid (fst (release s owner id)).
Proof.
  intros Hvalid. unfold release.
  destruct (reservations s !! id) as [entry|] eqn:Hlookup; cbn [fst].
  - destruct (decide (reservation_owner entry = owner)) as [Howner|Hwrong]; cbn [fst].
    + destruct Hvalid as [Hpositive [Hused Hsize]].
      cbn [valid with_records reservations capacity max_records]. split.
      * intros id' entry' Hafter. apply lookup_delete_Some in Hafter.
        destruct Hafter as [_ Hbefore]. eapply Hpositive. exact Hbefore.
      * split.
        -- pose proof (used_delete (reservations s) id entry Hlookup) as Haccounting.
           lia.
        -- rewrite map_size_delete_Some by (exists entry; exact Hlookup). lia.
    + exact Hvalid.
  - exact Hvalid.
Qed.

Lemma reserve_rejected_unchanged s r next error :
  reserve s r = (next, inl error) -> next = s.
Proof.
  unfold reserve. destruct (decide (request_amount r = 0)).
  - intros Hresult. inversion Hresult. reflexivity.
  - destruct (reservations s !! request_id r).
    + intros Hresult. inversion Hresult. reflexivity.
    + repeat case_decide; intros Hresult; inversion Hresult; reflexivity.
Qed.

Lemma release_rejected_unchanged s owner id next error :
  release s owner id = (next, inl error) -> next = s.
Proof.
  unfold release. destruct (reservations s !! id).
  - case_decide; intros Hresult; inversion Hresult; reflexivity.
  - intros Hresult. inversion Hresult. reflexivity.
Qed.

Lemma reserve_sequence_valid s requests final :
  valid s -> reserve_sequence s requests = inr final -> valid final.
Proof.
  revert s final. induction requests as [|r rest IH]; intros s final Hvalid Hrun.
  - cbn [reserve_sequence] in Hrun. inversion Hrun. subst. exact Hvalid.
  - cbn [reserve_sequence] in Hrun.
    destruct (reserve s r) as [next outcome] eqn:Hstep.
    destruct outcome as [error|u]; [discriminate|].
    destruct (reserve_sequence next rest) as [[index error]|last] eqn:Hrest;
      [discriminate|].
    inversion Hrun. subst last.
    apply (IH next final).
    + pose proof (reserve_valid s r Hvalid) as Hnext.
      rewrite Hstep in Hnext. exact Hnext.
    + exact Hrest.
Qed.

Lemma reserve_batch_valid s requests :
  valid s -> valid (fst (reserve_batch s requests)).
Proof.
  intros Hvalid. unfold reserve_batch. case_decide; cbn [fst].
  - destruct (reserve_sequence s requests) as [[index error]|final] eqn:Hrun;
      cbn [fst].
    + exact Hvalid.
    + eapply reserve_sequence_valid; eassumption.
  - exact Hvalid.
Qed.

Lemma reserve_batch_rejected_unchanged s requests next error :
  reserve_batch s requests = (next, inl error) -> next = s.
Proof.
  unfold reserve_batch. case_decide.
  - destruct (reserve_sequence s requests) as [[index cause]|final];
      intros Hresult; inversion Hresult; reflexivity.
  - intros Hresult. inversion Hresult. reflexivity.
Qed.
End resource_ledger.
