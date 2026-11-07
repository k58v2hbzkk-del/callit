use solana_sdk::{pubkey::Pubkey, transaction::Transaction};
use std::str::FromStr;
#[test]
fn python_built_settle_tx_is_valid() {
    let hex = include_str!("pytx.hex").trim();
    let bytes: Vec<u8> = (0..hex.len()).step_by(2).map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap()).collect();
    let tx: Transaction = bincode::deserialize(&bytes).unwrap();
    tx.verify().expect("signature must verify");
    use solana_sdk::sanitize::Sanitize; tx.sanitize().expect("well-formed");
    let m = &tx.message;
    assert_eq!(m.account_keys[0], Pubkey::from_str("9fhzt61u3YzUWYRY66E3ArvJVS2We8jvaHVuSLuUdT9Q").unwrap());
    assert_eq!(m.header.num_required_signatures, 1);
    let ix = &m.instructions[0];
    assert_eq!(ix.data, vec![2, 1]);
    assert_eq!(ix.accounts.len(), 8);
    assert_eq!(m.account_keys[ix.program_id_index as usize], Pubkey::from_str("G7J3iG9Kunj75GPu2hDnYnPDgto1Qp68XFT2GYsafzPe").unwrap());
    // writable flags: bet/creator/opponent/vault/atas writable, token program + program readonly
    for (pos, w) in [(1, true), (2, true), (3, true), (4, true), (7, false)] {
        assert_eq!(m.is_maybe_writable(ix.accounts[pos] as usize, None), w, "account {pos}");
    }
}
