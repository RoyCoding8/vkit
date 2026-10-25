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

Fixpoint active_pairs (slots : list (option (Id * reservation))) :
    list (Id * reservation) :=
  match slots with
  | [] => []
  | None :: rest => active_pairs rest
  | Some item :: rest => item :: active_pairs rest
  end.

Definition slot_records (slots : list (option (Id * reservation))) :
    gmap Id reservation := list_to_map (active_pairs slots).

Definition slot_total (slots : list (option (Id * reservation))) : nat :=
  foldr (fun pair total => reservation_amount (snd pair) + total) 0 (active_pairs slots).

Definition slot_view (s : ledger) (slots : list (option (Id * reservation))) : Prop :=
  length slots = Pos.to_nat (max_records s) /\
  NoDup ((active_pairs slots).*1) /\ slot_records slots = reservations s.

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

Inductive command :=
  | CmdReserve (r : request)
  | CmdRelease (owner : Owner) (id : Id)
  | CmdBatch (requests : list request).

Definition response (c : command) : Type :=
  match c with
  | CmdReserve _ => reserve_error + unit
  | CmdRelease _ _ => release_error + nat
  | CmdBatch _ => batch_error + unit
  end.

Inductive outputs : list command -> Type :=
  | outputs_nil : outputs []
  | outputs_cons {c cs} : response c -> outputs cs -> outputs (c :: cs).

Definition perform (s : ledger) (c : command) : ledger * response c :=
  match c as c0 return ledger * response c0 with
  | CmdReserve r => reserve s r
  | CmdRelease owner id => release s owner id
  | CmdBatch requests => reserve_batch s requests
  end.

Fixpoint run_commands (s : ledger) (cs : list command) : ledger * outputs cs :=
  match cs as cs0 return ledger * outputs cs0 with
  | [] => (s, outputs_nil)
  | c :: rest =>
      let '(next, out) := perform s c in
      let '(final, outs) := run_commands next rest in
      (final, outputs_cons out outs)
  end.

Inductive faithful : ledger -> forall cs, outputs cs -> ledger -> Prop :=
  | faithful_nil s : faithful s [] outputs_nil s
  | faithful_cons s c cs next out final outs :
      perform s c = (next, out) -> faithful next cs outs final ->
      faithful s (c :: cs) (outputs_cons out outs) final.

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

Lemma active_pairs_length slots : length (active_pairs slots) <= length slots.
Proof.
  induction slots as [|slot slots IH]; [simpl; lia|].
  destruct slot; simpl; lia.
Qed.

Lemma slot_records_size slots :
  NoDup ((active_pairs slots).*1) ->
  size (slot_records slots) = length (active_pairs slots).
Proof. intros Hnd. unfold slot_records. apply map_size_list_to_map. exact Hnd. Qed.

Lemma used_list_to_map (pairs : list (Id * reservation)) :
  NoDup (pairs.*1) ->
  used (list_to_map pairs) =
    foldr (fun pair total => reservation_amount (snd pair) + total) 0 pairs.
Proof.
  induction pairs as [|[id entry] rest IH]; intros Hnd.
  - rewrite list_to_map_nil, used_empty. reflexivity.
  - inversion Hnd as [|? ? Hfresh Htail].
    rewrite list_to_map_cons.
    assert (Hnone : (list_to_map rest : gmap Id reservation) !! id = None).
    { apply not_elem_of_list_to_map_1. exact Hfresh. }
    rewrite used_insert by exact Hnone.
    change (reservation_amount entry + used (list_to_map rest) =
      reservation_amount entry +
        foldr (fun pair total => reservation_amount (snd pair) + total) 0 rest).
    f_equal. apply IH. exact Htail.
Qed.

Lemma used_slot_records slots :
  NoDup ((active_pairs slots).*1) -> used (slot_records slots) = slot_total slots.
Proof.
  intros Hnd. unfold slot_records, slot_total. apply used_list_to_map. exact Hnd.
Qed.

Lemma slot_view_size_bound s slots :
  slot_view s slots -> size (reservations s) <= Pos.to_nat (max_records s).
Proof.
  intros [Hlen [Hnd Hmap]]. rewrite <- Hmap, slot_records_size by exact Hnd.
  eapply Nat.le_trans; [apply active_pairs_length|]. rewrite Hlen. lia.
Qed.

Lemma slot_cache_matches_map s slots cached_used :
  slot_view s slots -> cached_used = slot_total slots ->
  cached_used = used (reservations s).
Proof.
  intros [_ [Hnd Hmap]] Hcache. rewrite <- Hmap.
  rewrite used_slot_records by exact Hnd. exact Hcache.
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
           set (new_entry := {| reservation_owner := request_owner r;
                                reservation_amount := request_amount r |}).
           change
             ((forall id entry,
                (<[request_id r := new_entry]> (reservations s)) !! id = Some entry ->
                0 < reservation_amount entry) /\
              used (<[request_id r := new_entry]> (reservations s)) <= capacity s /\
              size (<[request_id r := new_entry]> (reservations s)) <=
                Pos.to_nat (max_records s)).
           split.
           ++ intros id entry Hnew. apply lookup_insert_Some in Hnew.
              destruct Hnew as [[_ Hentry]|[_ Hold]].
              ** subst entry. change (0 < request_amount r). lia.
              ** eapply Hpositive. exact Hold.
           ++ split.
              ** rewrite used_insert by exact Hlookup.
                 change (request_amount r + used (reservations s) <= capacity s). lia.
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
      change
        ((forall id' entry',
            delete id (reservations s) !! id' = Some entry' ->
            0 < reservation_amount entry') /\
         used (delete id (reservations s)) <= capacity s /\
         size (delete id (reservations s)) <= Pos.to_nat (max_records s)).
      split.
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
Lemma reserve_success_accounting s r next :
  reserve s r = (next, inr tt) ->
  0 < request_amount r /\
  reservations s !! request_id r = None /\
  size (reservations s) < Pos.to_nat (max_records s) /\
  request_amount r <= capacity s - used (reservations s) /\
  reservations next =
    <[request_id r := {| reservation_owner := request_owner r;
                        reservation_amount := request_amount r |}]> (reservations s) /\
  used (reservations next) = request_amount r + used (reservations s) /\
  size (reservations next) = S (size (reservations s)) /\
  capacity next = capacity s /\ max_records next = max_records s.
Proof.
  unfold reserve. destruct (decide (request_amount r = 0)); [discriminate|].
  destruct (reservations s !! request_id r) as [entry|] eqn:Hlookup; [discriminate|].
  destruct (decide (size (reservations s) < Pos.to_nat (max_records s)));
    [|discriminate].
  destruct (decide (request_amount r <= capacity s - used (reservations s)));
    [|discriminate].
  intros Hresult. inversion Hresult. subst next.
  set (new_entry := {| reservation_owner := request_owner r;
                       reservation_amount := request_amount r |}).
  change
    (0 < request_amount r /\ None = (None : option reservation) /\
     size (reservations s) < Pos.to_nat (max_records s) /\
     request_amount r <= capacity s - used (reservations s) /\
     <[request_id r := new_entry]> (reservations s) =
       <[request_id r := new_entry]> (reservations s) /\
     used (<[request_id r := new_entry]> (reservations s)) =
       request_amount r + used (reservations s) /\
     size (<[request_id r := new_entry]> (reservations s)) = S (size (reservations s)) /\
     capacity s = capacity s /\ max_records s = max_records s).
  repeat split; try assumption; try reflexivity; try lia.
  - rewrite used_insert by exact Hlookup. reflexivity.
  - rewrite map_size_insert_None by exact Hlookup. reflexivity.
Qed.

Lemma release_success_accounting s owner id next amount :
  release s owner id = (next, inr amount) ->
  exists entry,
    reservations s !! id = Some entry /\ reservation_owner entry = owner /\
    amount = reservation_amount entry /\ reservations next = delete id (reservations s) /\
    used (reservations s) = amount + used (reservations next) /\
    size (reservations next) = pred (size (reservations s)) /\
    capacity next = capacity s /\ max_records next = max_records s.
Proof.
  unfold release. destruct (reservations s !! id) as [entry|] eqn:Hlookup;
    [|discriminate].
  destruct (decide (reservation_owner entry = owner)); [|discriminate].
  intros Hresult. inversion Hresult. subst next amount. exists entry.
  change
    (Some entry = Some entry /\ reservation_owner entry = owner /\
     reservation_amount entry = reservation_amount entry /\
     delete id (reservations s) = delete id (reservations s) /\
     used (reservations s) = reservation_amount entry + used (delete id (reservations s)) /\
     size (delete id (reservations s)) = pred (size (reservations s)) /\
     capacity s = capacity s /\ max_records s = max_records s).
  repeat split; try assumption; try reflexivity.
  - apply used_delete. exact Hlookup.
  - rewrite map_size_delete_Some by (exists entry; exact Hlookup). reflexivity.
Qed.
Lemma reserve_sequence_first_failure s requests index error :
  reserve_sequence s requests = inl (index, error) ->
  exists prefix r suffix mid,
    requests = prefix ++ r :: suffix /\ length prefix = index /\
    reserve_sequence s prefix = inr mid /\ reserve mid r = (mid, inl error).
Proof.
  revert s index error.
  induction requests as [|r rest IH]; intros s index error Hrun; [discriminate|].
  cbn [reserve_sequence] in Hrun.
  destruct (reserve s r) as [next outcome] eqn:Hstep.
  destruct outcome as [cause|u].
  - inversion Hrun. subst index error. exists [], r, rest, s.
    repeat split; try reflexivity.
    assert (next = s) by (eapply reserve_rejected_unchanged; exact Hstep).
    subst next. exact Hstep.
  - destruct (reserve_sequence next rest) as [[i cause]|last] eqn:Htail;
      [|discriminate].
    inversion Hrun. subst index error.
    destruct (IH next i cause Htail) as (prefix & bad & suffix & mid & Hlist & Hlen & Hprefix & Hbad).
    exists (r :: prefix), bad, suffix, mid. repeat split.
    + cbn. rewrite Hlist. reflexivity.
    + cbn. rewrite Hlen. reflexivity.
    + cbn [reserve_sequence]. rewrite Hstep, Hprefix. reflexivity.
    + exact Hbad.
Qed.

Lemma reserve_batch_first_failure s requests next index error :
  reserve_batch s requests = (next, inl (BatchFailure index error)) ->
  next = s /\ length requests <= Pos.to_nat batch_bound /\
  exists prefix r suffix mid,
    requests = prefix ++ r :: suffix /\ length prefix = index /\
    reserve_sequence s prefix = inr mid /\ reserve mid r = (mid, inl error).
Proof.
  unfold reserve_batch. case_decide.
  - destruct (reserve_sequence s requests) as [[i cause]|last] eqn:Hsequence;
      [|discriminate].
    intros Hrun. inversion Hrun. subst next index error.
    split; [reflexivity|]. split; [assumption|].
    eapply reserve_sequence_first_failure. exact Hsequence.
  - discriminate.
Qed.
Lemma reserve_rejection_spec s r error :
  reserve s r = (s, inl error) <->
  match error with
  | ZeroAmount => request_amount r = 0
  | DuplicateId => request_amount r <> 0 /\ is_Some (reservations s !! request_id r)
  | NoRecordSpace =>
      request_amount r <> 0 /\ reservations s !! request_id r = None /\
      Pos.to_nat (max_records s) <= size (reservations s)
  | InsufficientCapacity =>
      request_amount r <> 0 /\ reservations s !! request_id r = None /\
      size (reservations s) < Pos.to_nat (max_records s) /\
      capacity s - used (reservations s) < request_amount r
  end.
Proof.
  destruct error; unfold reserve;
    destruct (decide (request_amount r = 0));
    destruct (reservations s !! request_id r) as [entry|] eqn:Hlookup;
    repeat case_decide; cbn; try unfold is_Some; naive_solver lia.
Qed.

Lemma release_rejection_spec s owner id error :
  release s owner id = (s, inl error) <->
  match error with
  | MissingId => reservations s !! id = None
  | WrongOwner => exists entry,
      reservations s !! id = Some entry /\ reservation_owner entry <> owner
  end.
Proof.
  destruct error; unfold release;
    destruct (reservations s !! id) as [entry|] eqn:Hlookup;
    repeat case_decide; cbn; naive_solver.
Qed.
Lemma reserve_sequence_accounting s requests final :
  reserve_sequence s requests = inr final ->
  used (reservations final) =
    foldr (fun r total => request_amount r + total) 0 requests + used (reservations s) /\
  size (reservations final) = length requests + size (reservations s) /\
  capacity final = capacity s /\ max_records final = max_records s.
Proof.
  revert s final. induction requests as [|r rest IH]; intros s final Hrun.
  - cbn [reserve_sequence] in Hrun. inversion Hrun. subst final.
    repeat split; reflexivity.
  - cbn [reserve_sequence] in Hrun.
    destruct (reserve s r) as [next outcome] eqn:Hstep.
    destruct outcome as [error|[]]; [discriminate|].
    destruct (reserve_sequence next rest) as [[index error]|last] eqn:Htail;
      [discriminate|].
    inversion Hrun. subst last.
    destruct (reserve_success_accounting s r next Hstep)
      as (_ & _ & _ & _ & _ & Hused & Hsize & Hcap & Hmax).
    destruct (IH next final Htail) as (Htailused & Htailsize & Htailcap & Htailmax).
    cbn [foldr length]. repeat split.
    + rewrite Htailused, Hused. lia.
    + rewrite Htailsize, Hsize. lia.
    + rewrite Htailcap, Hcap. reflexivity.
    + rewrite Htailmax, Hmax. reflexivity.
Qed.

Lemma reserve_batch_success_accounting s requests final :
  reserve_batch s requests = (final, inr tt) ->
  length requests <= Pos.to_nat batch_bound /\
  used (reservations final) =
    foldr (fun r total => request_amount r + total) 0 requests + used (reservations s) /\
  size (reservations final) = length requests + size (reservations s) /\
  capacity final = capacity s /\ max_records final = max_records s.
Proof.
  unfold reserve_batch. case_decide.
  - destruct (reserve_sequence s requests) as [[index error]|last] eqn:Hsequence;
      [discriminate|].
    intros Hrun. inversion Hrun. subst last.
    split; [assumption|]. apply reserve_sequence_accounting. exact Hsequence.
  - discriminate.
Qed.

Lemma reserve_batch_too_large s requests :
  reserve_batch s requests = (s, inl BatchTooLarge) <->
  Pos.to_nat batch_bound < length requests.
Proof.
  unfold reserve_batch. case_decide.
  - destruct (reserve_sequence s requests) as [[index error]|final]; naive_solver lia.
  - naive_solver lia.
Qed.
Lemma perform_valid s c : valid s -> valid (fst (perform s c)).
Proof.
  destruct c; cbn [perform].
  - apply reserve_valid.
  - apply release_valid.
  - apply reserve_batch_valid.
Qed.

Lemma run_commands_correct s cs final outs :
  valid s -> run_commands s cs = (final, outs) ->
  valid final /\ faithful s cs outs final.
Proof.
  revert s final outs. induction cs as [|c rest IH]; intros s final outs Hvalid Hrun.
  - cbn [run_commands] in Hrun. inversion Hrun. subst.
    split; [exact Hvalid|constructor].
  - cbn [run_commands] in Hrun.
    destruct (perform s c) as [next out] eqn:Hstep.
    destruct (run_commands next rest) as [last tail] eqn:Htail.
    inversion Hrun. subst final outs.
    pose proof (perform_valid s c Hvalid) as Hnext.
    rewrite Hstep in Hnext. cbn [fst] in Hnext.
    destruct (IH next last tail Hnext Htail) as [Hfinal Hfaithful].
    split; [exact Hfinal|]. econstructor; eassumption.
Qed.
End resource_ledger.
