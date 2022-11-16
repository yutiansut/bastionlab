use polars::prelude::*;
use serde_json;
use std::{
    collections::{HashMap, HashSet},
    error::Error,
    sync::{Arc, Mutex, RwLock},
};
use tokio_stream::wrappers::ReceiverStream;
use tonic::{metadata::KeyRef, transport::Server, Request, Response, Status, Streaming};
use uuid::Uuid;

pub mod grpc {
    tonic::include_proto!("bastionlab");
}
use grpc::{
    bastion_lab_server::{BastionLab, BastionLabServer},
    ChallengeResponse, Chunk, Empty, Query, ReferenceRequest, ReferenceResponse,
};

mod serialization;
use serialization::*;

mod composite_plan;
use composite_plan::*;

mod access_control;
use access_control::*;

use ring::rand;
#[derive(Debug, Default)]
pub struct BastionLabState {
    // queries: Arc<Vec<String>>,
    dataframes: Arc<RwLock<HashMap<String, DataFrame>>>,
    keys: Mutex<KeyManagement>,
    challenges: Mutex<HashSet<[u8; 32]>>,
}

impl BastionLabState {
    fn new(keys: KeyManagement) -> Self {
        Self {
            // queries: Arc::new(Vec::new()),
            dataframes: Arc::new(RwLock::new(HashMap::new())),
            keys: Mutex::new(keys),
            challenges: Default::default(),
        }
    }

    fn get_df(&self, identifier: &str) -> Result<DataFrame, Status> {
        let dfs = self.dataframes.read().unwrap();
        Ok(dfs
            .get(identifier)
            .ok_or(Status::not_found(format!(
                "Could not find dataframe: identifier={}",
                identifier
            )))?
            .clone())
    }

    fn verify_request<T>(&self, request: &Request<T>) -> Result<(), Status> {
        let pat = "signing-key-";
        for key in request.metadata().keys() {
            match key {
                KeyRef::Binary(key) => {
                    let key = key.to_string();
                    if let Some(key) = key.strip_suffix("-bin") {
                        if key.contains(pat) {
                            if let Some(key) = key.split(pat).last() {
                                let lock = self.keys.lock().unwrap();
                                lock.verify_key(key)?;
                            }
                            println!("key: {:?}", key);
                        }
                    }
                }
                _ => (),
            }
        }

        Ok(())
    }

    // fn get_dfs(&self, identifiers: &[String]) -> Result<VecDeque<DataFrame>, Status> {
    //     let dfs = self.dataframes.read().unwrap();
    //     let mut res = VecDeque::with_capacity(identifiers.len());
    //     for identifier in identifiers.iter() {
    //         res.push_back(dfs.get(identifier).ok_or(Status::not_found(format!("Could not find dataframe: identifier={}", identifier)))?.clone());
    //     }
    //     Ok(res)
    // }

    fn insert_df(&self, df: DataFrame) -> String {
        let mut dfs = self.dataframes.write().unwrap();
        let identifier = format!("{}", Uuid::new_v4());
        dfs.insert(identifier.clone(), df);
        identifier
    }
    fn check_challenge<T>(&self, request: &Request<T>) -> Result<(), Status> {
        if let Some(meta) = request.metadata().get_bin("challenge-bin") {
            let challenge = meta
                .to_bytes()
                .map_err(|_| Status::invalid_argument("Could not decode challenge"))?;
            let mut lock = self.challenges.lock().unwrap();
            if !lock.remove(challenge.as_ref()) {
                Err(Status::permission_denied("Invalid or reused challenge"))?
            }
        }
        Ok(())
    }

    fn new_challenge(&self) -> [u8; 32] {
        let rng = rand::SystemRandom::new();
        loop {
            let challenge: [u8; 32] = rand::generate(&rng)
                .expect("Could not generate random value")
                .expose();
            if self.challenges.lock().unwrap().insert(challenge) {
                return challenge;
            }
        }
    }
}

#[tonic::async_trait]
impl BastionLab for BastionLabState {
    type FetchDataFrameStream = ReceiverStream<Result<Chunk, Status>>;

    async fn run_query(
        &self,
        request: Request<Query>,
    ) -> Result<Response<ReferenceResponse>, Status> {
        // let input_dfs = self.get_dfs(&request.get_ref().identifiers)?;
        println!("{:?}", request);
        println!("{}", &request.get_ref().composite_plan);
        let composite_plan: CompositePlan = serde_json::from_str(&request.get_ref().composite_plan)
            .map_err(|e| {
                Status::invalid_argument(format!(
                    "Could not deserialize composite plan: {}{}",
                    e,
                    &request.get_ref().composite_plan
                ))
            })?;
        let res = composite_plan.run(self)?;

        let header = serde_json::to_string(&res.schema()).map_err(|e| {
            Status::internal(format!(
                "Could not serialize result data frame header: {}",
                e
            ))
        })?;
        let identifier = self.insert_df(res);
        Ok(Response::new(ReferenceResponse { identifier, header }))
    }

    async fn send_data_frame(
        &self,
        request: Request<Streaming<Chunk>>,
    ) -> Result<Response<ReferenceResponse>, Status> {
        let df = df_from_stream(request.into_inner()).await?;

        let header = serde_json::to_string(&df.schema())
            .map_err(|e| Status::internal(format!("Could not serialize header: {}", e)))?;
        let identifier = self.insert_df(df);
        Ok(Response::new(ReferenceResponse { identifier, header }))
    }

    async fn fetch_data_frame(
        &self,
        request: Request<ReferenceRequest>,
    ) -> Result<Response<Self::FetchDataFrameStream>, Status> {
        self.check_challenge(&request)?;
        self.verify_request(&request)?;
        let df = self.get_df(&request.get_ref().identifier)?;

        Ok(stream_data(df, 32).await)
    }

    async fn get_challenge(
        &self,
        _request: Request<Empty>,
    ) -> Result<Response<ChallengeResponse>, Status> {
        let challenge = self.new_challenge();
        Ok(Response::new(ChallengeResponse {
            value: challenge.into(),
        }))
    }
}
#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    let keys = KeyManagement::load_from_dir("./keys".to_string())?;
    let state = BastionLabState::new(keys);
    let addr = "[::1]:50056".parse()?;
    println!("BastionLab server running...");

    // println!("{:?}", serde_json::from_str::<CompositePlan>("[{\"EntryPointPlanSegment\":\"1da61d9a-c8a8-4e8e-baec-b132db9009d9\"},{\"EntryPointPlanSegment\":\"1da61d9a-c8a8-4e8e-baec-b132db9009d9\"}]").unwrap());
    Server::builder()
        .add_service(BastionLabServer::new(state))
        .serve(addr)
        .await?;
    Ok(())
}
