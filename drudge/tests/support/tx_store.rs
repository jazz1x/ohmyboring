//! Test-side convenience: the verdict write functions take a transaction handle, and a test that
//! drives one call wants it opened, run and committed in one line. Same names as the `Store`
//! functions, so a call reads `store.record_consumption(..)` and keeps its assertions.
#![allow(dead_code, async_fn_in_trait)]

use anyhow::Result;
use drudge::store::{ConsumptionReport, HandoverReport, Store, SupersedesReport};

pub trait StoreTx {
    async fn record_consumption(
        &self,
        session_id: &str,
        observed_at: &str,
        used: &[String],
        contested: &[String],
        supersedes: &[[String; 2]],
        judge: Option<&str>,
    ) -> Result<ConsumptionReport>;
    async fn record_handover(
        &self,
        session_id: &str,
        observed_at: &str,
        paths: &[String],
    ) -> Result<HandoverReport>;
    async fn record_supersedes(
        &self,
        pairs: &[[String; 2]],
        judge: Option<&str>,
    ) -> Result<SupersedesReport>;
    async fn handed_paths(&self, session_id: &str) -> Result<Vec<String>>;
    async fn owner_authored(&self, paths: &[String]) -> Result<Vec<String>>;
}

impl StoreTx for Store {
    async fn record_consumption(
        &self,
        session_id: &str,
        observed_at: &str,
        used: &[String],
        contested: &[String],
        supersedes: &[[String; 2]],
        judge: Option<&str>,
    ) -> Result<ConsumptionReport> {
        let mut conn = self.conn().await?;
        let tx = conn.transaction().await?;
        let report = Store::record_consumption(
            &tx,
            session_id,
            observed_at,
            used,
            contested,
            supersedes,
            judge,
        )
        .await?;
        tx.commit().await?;
        Ok(report)
    }

    async fn record_handover(
        &self,
        session_id: &str,
        observed_at: &str,
        paths: &[String],
    ) -> Result<HandoverReport> {
        self.handover_in_tx(session_id, observed_at, paths).await
    }

    async fn record_supersedes(
        &self,
        pairs: &[[String; 2]],
        judge: Option<&str>,
    ) -> Result<SupersedesReport> {
        self.supersedes_in_tx(pairs, judge).await
    }

    async fn handed_paths(&self, session_id: &str) -> Result<Vec<String>> {
        let mut conn = self.conn().await?;
        let tx = conn.transaction().await?;
        let paths = Store::handed_paths(&tx, session_id).await?;
        tx.commit().await?;
        Ok(paths)
    }

    async fn owner_authored(&self, paths: &[String]) -> Result<Vec<String>> {
        let mut conn = self.conn().await?;
        let tx = conn.transaction().await?;
        let owned = Store::owner_authored(&tx, paths).await?;
        tx.commit().await?;
        Ok(owned)
    }
}
