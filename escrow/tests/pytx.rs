use solana_sdk::{pubkey::Pubkey, sanitize::Sanitize, transaction::Transaction};
use std::str::FromStr;
#[test]
fn python_built_settle_tx_is_valid() {
    let hex = include_str!("pytx.hex").trim();
    let bytes: Vec<u8> = (0..hex.len()).step_by(2).map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap()).collect();
    let tx: Transaction = bincode::deserialize(&bytes).unwrap();
    tx.verify().expect("signature must verify");
    tx.sanitize().expect("well-formed");
    let m = &tx.message;
    let ix = &m.instructions[0];
    assert_eq!(ix.data, vec![2, 1]);
    assert_eq!(ix.accounts.len(), 4);
    assert_eq!(m.account_keys[ix.program_id_index as usize], Pubkey::from_str("FGJphifTMxBzKkhnGsxW9wBnw3bzrExuasMxLxdhzQox").unwrap());
    for pos in 1..4 { assert!(m.is_maybe_writable(ix.accounts[pos] as usize, None)); }
}
