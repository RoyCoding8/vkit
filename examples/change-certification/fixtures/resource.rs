#[derive(Clone, Copy)]
struct Pool {
    occupied: [bool; 3],
}

impl Pool {
    fn new() -> Self {
        Self { occupied: [false; 3] }
    }

    fn allocate_before(&mut self) -> Option<u8> {
        for index in 0..self.occupied.len() {
            if !self.occupied[index] {
                self.occupied[index] = true;
                return Some(index as u8);
            }
        }
        None
    }

    fn allocate_after(&mut self) -> Option<u8> {
        let index = self.occupied.iter().position(|occupied| !occupied)?;
        self.occupied[index] = true;
        Some(index as u8)
    }

    fn release(&mut self, id: u8) -> bool {
        let Some(occupied) = self.occupied.get_mut(usize::from(id)) else {
            return false;
        };
        let was_occupied = *occupied;
        *occupied = false;
        was_occupied
    }

    fn state(&self) -> String {
        self.occupied.iter().map(|occupied| if *occupied { '1' } else { '0' }).collect()
    }
}

struct OwnedPool {
    owners: [Option<u8>; 3],
}

impl OwnedPool {
    fn new() -> Self {
        Self { owners: [None; 3] }
    }

    fn allocate(&mut self, owner: u8) -> Option<u8> {
        let index = self.owners.iter().position(Option::is_none)?;
        self.owners[index] = Some(owner);
        Some(index as u8)
    }

    fn release_before(&mut self, slot: u8) -> bool {
        let Some(owner) = self.owners.get_mut(usize::from(slot)) else {
            return false;
        };
        owner.take().is_some()
    }

    fn release_after(&mut self, slot: u8, caller: u8) -> bool {
        let Some(owner) = self.owners.get_mut(usize::from(slot)) else {
            return false;
        };
        if *owner != Some(caller) {
            return false;
        }
        *owner = None;
        true
    }

    fn state(&self) -> String {
        if self.owners.iter().all(Option::is_none) {
            return "empty".to_owned();
        }
        self.owners.iter().enumerate()
            .map(|(slot, owner)| format!("{slot}={owner:?}"))
            .collect::<Vec<_>>()
            .join(",")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rs_01_fixed_pool_allocation_refactor() {
        let mut before = Pool::new();
        let mut after = Pool::new();
        let mut before_allocations = Vec::new();
        let mut after_allocations = Vec::new();
        for _ in 0..4 {
            before_allocations.push(before.allocate_before().map(|id| id.to_string()).unwrap_or_else(|| "None".to_owned()));
            after_allocations.push(after.allocate_after().map(|id| id.to_string()).unwrap_or_else(|| "None".to_owned()));
        }
        assert_eq!(before_allocations, ["0", "1", "2", "None"]);
        assert_eq!(after_allocations, ["0", "1", "2", "None"]);
        assert!(before.release(1));
        assert!(after.release(1));
        before_allocations.push(before.allocate_before().map(|id| id.to_string()).unwrap_or_else(|| "None".to_owned()));
        after_allocations.push(after.allocate_after().map(|id| id.to_string()).unwrap_or_else(|| "None".to_owned()));
        assert_eq!(before_allocations.last().map(String::as_str), Some("1"));
        assert_eq!(after_allocations.last().map(String::as_str), Some("1"));
        let before_observation = format!("{};{}", before_allocations.join(","), before.state());
        let after_observation = format!("{};{}", after_allocations.join(","), after.state());
        assert_eq!(before_observation, "0,1,2,None,1;111");
        assert_eq!(after_observation, "0,1,2,None,1;111");
        eprintln!("OBS|RS-01|allocate,allocate,allocate,allocate,release(1),allocate|before={before_observation}|after={after_observation}");
    }

    #[test]
    fn rs_02_owner_guarded_release_feature() {
        let mut before = OwnedPool::new();
        let mut after = OwnedPool::new();
        assert_eq!(before.allocate(7), Some(0));
        assert_eq!(after.allocate(7), Some(0));
        let before_wrong_owner = before.release_before(0);
        let before_owner = before.release_before(0);
        let before_repeat = before.release_before(0);
        let after_wrong_owner = after.release_after(0, 8);
        let after_owner = after.release_after(0, 7);
        let after_repeat = after.release_after(0, 7);
        let before_observation = format!("wrong-owner={before_wrong_owner},owner={before_owner},repeat={before_repeat};{}", before.state());
        let after_observation = format!("wrong-owner={after_wrong_owner},owner={after_owner},repeat={after_repeat};{}", after.state());
        assert_eq!(before_observation, "wrong-owner=true,owner=false,repeat=false;empty");
        assert_eq!(after_observation, "wrong-owner=false,owner=true,repeat=false;empty");
        eprintln!("OBS|RS-02|allocate(owner=7),release(slot=0,caller=8),release(slot=0,caller=7),release(slot=0,caller=7)|before={before_observation}|after={after_observation}");
    }
}
