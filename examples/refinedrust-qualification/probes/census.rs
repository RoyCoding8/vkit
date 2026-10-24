pub struct CensusRecord;

pub trait CensusTrait {
    fn required(&self);

    fn defaulted(&self) {}
}

impl CensusRecord {
    pub fn method(&self) {}
}

unsafe extern "C" {
    fn external_call();
}

pub fn closure_owner() {
    let _unused = || 1u32;
}
